from itertools import combinations
import numpy as np
import pandas as pd
import jax.numpy as jnp
import jax
from jax import random
from jax.nn import (relu, log_softmax, softmax, softplus, sigmoid, elu,
                    leaky_relu, selu, gelu, normalize)
from jax.nn.initializers import glorot_normal, normal, ones, zeros
from jax.experimental.optimizers import l2_norm
from UAT.models.models import (AttentionModel_MAP, AttentionModel_Dropout,
                               AttentionModel_Variational, AttentionModel_LastLayer,
                               EnsembleModel)
from UAT.training.train import training_loop
from UAT.uncertainty.laplace import laplace_approximation
from UAT.models.layers import NeuralNet as nn

""" 
Scikit wrappers for models where they are initialized with dicts of kwargs for defining:
- the model
- training parameters (batch_size, epochs, lr)
- last layer uncertainty estimation parameters 
"""


class UAT:
    def __init__(
        self,
        model_kwargs,
        training_kwargs,
        loss_fun,
        metric_fun=None,
        rng_key=42,
        feat_names=None,
        posterior_params={"name":"MAP"},
        ):
        
        """
        model_kwargs: dict, dict(
                        features=features,
                        d_model=32,
                        embed_hidden_size=64,
                        embed_hidden_layers=5,
                        embed_activation=relu,
                        encoder_layers=10,
                        encoder_heads=5,
                        enc_activation=relu,
                        decoder_layers=10,
                        decoder_heads=5,
                        dec_activation=relu,
                        net_hidden_size=64,
                        net_hidden_layers=5,
                        net_activation=relu,
                        last_layer_size=64,
                        out_size=1,
                        W_init = glorot_normal(),
                        b_init = zeros,
                        temp = 0.1,
                        eps = 1e-7,
                    )
        training_kwargs: dict, dict(
                        batch_size=32,
                        epochs=100,
                        lr=1e-3,
                    )
        loss_fun: callable, takes (params, output, labels) 
                            and return (loss: scalar, loss_dict: dict for metric monitoring)
        """
        key = jax.random.PRNGKey(rng_key)
        key, init_key = random.split(key)
        
        print(posterior_params["name"])
        if posterior_params["name"] == "MAP":
            init_fun, apply_fun = AttentionModel_MAP(
                **model_kwargs
                )
            params = init_fun(init_key)
        
        if posterior_params["name"] == "Ensemble":
            init_fun, apply_fun = AttentionModel_MAP(
                **model_kwargs
                )
            ensemble_keys = random.split(init_key, posterior_params["m"])
            params = [init_fun(k) for k in ensemble_keys]
        
        if posterior_params["name"] == "Dropout":
            init_fun, apply_fun = AttentionModel_Dropout(
                **model_kwargs
                )
            params = init_fun(init_key)
        
        if posterior_params["name"] == "Variational":
            init_fun, apply_fun = AttentionModel_Variational(
                **model_kwargs
                )
            params = init_fun(init_key)
        
        self.params = params
        self.apply_fun = apply_fun
        
        self.key = key
        if (feat_names is not None) and len(feat_names) == model_kwargs["features"]:
            self.feat_names = feat_names
        else:
            self.feat_names = list(range(model_kwargs["features"]))
        
        self.model_kwargs = model_kwargs
        self.loss_fun = loss_fun
        if metric_fun is not None:
            self.metric_fun = metric_fun
        else:
            self.metric_fun = loss_fun
        self.posterior_params = posterior_params
        self.training_kwargs = training_kwargs

    def fit(self, X, y):
        if self.posterior_params["name"] in ["MAP", "Dropout", "Variational"]:
            params, history, rng = training_loop(
                X=X,
                y=y,
                model_fun=self.apply_fun,
                params=self.params,
                loss_fun=self.loss_fun,
                metric_fun=self.metric_fun,
                rng=self.key,
                **self.training_kwargs
                )
            self.params = params
            self._history = history
            self.key = rng

        if self.posterior_params["name"] == "laplace":
            params, rng = laplace_approximation(
                self.params,
                self.apply_fun,
                self.loss_fun,
                X,
                y,
                self.key,
                **self.posterior_params
            )
            self.params = params
            self.key = rng
    
    def predict_proba(self, X):
        if self.posterior_params["name"] == "MAP":
            rng_placeholder = jnp.ones((X.shape[0],2))
            out = self.apply_fun(self.params, X, rng_placeholder, False)
            logits = out[0]
            if logits.shape[1] > 1:
                probs = jax.nn.softmax(logits)
            else:
                probs = jax.nn.sigmoid(logits)
            
        
        if self.posterior_params["name"] == "Ensemble":
            probs = []
            for p in self.params:
                rng_placeholder = jnp.ones((X.shape[0],2))
                out = self.apply_fun(self.params, X, rng_placeholder, False)
                logits = out
                probs.append(jax.nn.sigmoid(logits))
            probs = jnp.mean(jnp.stack(probs, axis=0))
        

        return jnp.squeeze(probs)
    
    def predict(self, X):
        if self.posterior_params["name"] == "MAP":
            rng_placeholder = jnp.ones((X.shape[0],2))
            out = self.apply_fun(self.params, X, rng_placeholder, False)
            out = out[0]

        return jnp.squeeze(out)
    
    def distances(self, X, y):
        cols = []
        for f in range(1,len(self.feat_names) + 1):
            combs = list(combinations(list(range(len(self.feat_names))), f))
            # tuples to lists
            cols += [list(l) for l in combs]
        cols.append([])
        full_set = set(range(len(self.feat_names)))

        latent_spaces = []
        for c in cols:
            s = set(c)
            nan_set = full_set - s
            X_nan = X.copy()
            X_nan[:, list(nan_set)] = np.nan
            rng_placeholder = random.split(self.key, X_nan.shape[0])
            out = self.apply_fun(self.params, X_nan, rng_placeholder, False)
            z = out[-1]  # (batch, 1, dims)
            print(c, z.shape)
            latent_spaces.append(z)

        z = np.concatenate(latent_spaces, axis=1)  # (batch, sets, dims)
        z = z[y == 1, ...]
        # store noise and signal
        distances = []
        # store full comparison
        dist_full = []
        sets = [ set(l) for l in cols ]
        for i in range(len(cols)):
            euclid = np.sqrt(np.square(z[:, i:i+1, :] - z).sum(-1)).mean(0)  # (feat)
            dist_full.append(euclid)
            idx_noise = np.array([ True if (
                (len(s) > len(sets[i])) and (len(s - sets[i]) == 1) and (2 in s - sets[i])
                ) else False for s in sets ])
            idx_signal = np.array([ True if (
                (len(s) > len(sets[i])) and (len(s - sets[i]) == 1) and (3 in s - sets[i])
                ) else False for s in sets ])
            distances.append(
                np.array([euclid[idx_noise].mean(),  euclid[idx_signal].mean()])
            )

        distances = pd.DataFrame(
            np.stack(distances, axis=0),
            columns=["+noise", "+signal"],
            index=[str(s) for s in sets[:-1]] + ["{}"],
            )  # (feat, 2)
        dist_full = pd.DataFrame(
            np.stack(dist_full, axis=0),
            columns=[str(s) for s in sets[:-1]] + ["{}"],
            index=[str(s) for s in sets[:-1]] + ["{}"],
            )  # (feat, 2)
        return distances, dist_full
    
    def history(self):
        return self._history
    
    def get_params(self, deep=True):
        return {
            'model_kwargs':self.model_kwargs,
            'loss_fun':self.loss_fun,
            'posterior_params':self.posterior_params,
            'training_kwargs':self.training_kwargs
            }

    def set_params(self, **parameters):
        for parameter, value in parameters.items():
            setattr(self, parameter, value)
        self.reinit()
        return self
    
    def reinit(self):
        key, init_key = random.split(self.key)
        self.key = key
        if self.posterior_params["name"] == "MAP":
            init_fun, apply_fun = AttentionModel_MAP(
                **self.model_kwargs
                )
            params = init_fun(init_key)
        
        if self.posterior_params["name"] == "Ensemble":
            init_fun, apply_fun = AttentionModel_MAP(
                **self.model_kwargs
                )
            ensemble_keys = random.split(init_key, self.posterior_params["m"])
            params = [init_fun(k) for k in ensemble_keys]
        
        if self.posterior_params["name"] == "Dropout":
            init_fun, apply_fun = AttentionModel_Dropout(
                **self.model_kwargs
                )
            params = init_fun(init_key)
        
        if self.posterior_params["name"] == "Variational":
            init_fun, apply_fun = AttentionModel_Variational(
                **self.model_kwargs
                )
            params = init_fun(init_key)
        
        self.params = params
        self.apply_fun = apply_fun


class Ensemble:
    def __init__(
        self,
        model_kwargs,
        training_kwargs,
        loss_fun,
        rng_key=42,
        feat_names=None,
        ):
        
        init_fun, apply_fun, cols = EnsembleModel(
            **model_kwargs
            )
        key = jax.random.PRNGKey(rng_key)
        key, init_key = random.split(key)
        self.cols = cols
        self.key = key
        self.params = init_fun(init_key)
        self.apply_fun = apply_fun
        if (feat_names is not None) and len(feat_names) == model_kwargs["features"]:
            self.feat_names = feat_names
        else:
            self.feat_names = list(range(model_kwargs["features"]))
        self.loss_fun = loss_fun
        self.training_kwargs = training_kwargs

    def fit(self, X, y):

        params, history, rng = training_loop(
            X=X,
            y=y,
            model_fun=self.apply_fun,
            params=self.params,
            loss_fun=self.loss_fun,
            metric_fun=self.loss_fun,
            rng=self.key,
            **self.training_kwargs
            )
        self.params = params
        self.history = history
        self.key = rng
    
    def predict_proba(self, X):
        out = self.apply_fun(self.params, X, None)
        logits = out[0]
        probs = jnp.mean(jnp.stack(jax.nn.sigmoid(logits), axis=0), axis=0)
        return probs
    
    def predict(self, X):
        out = self.apply_fun(self.params, X, None)
        logits = out[0]
        probs = jnp.mean(jnp.stack(jax.nn.sigmoid(logits), axis=0), axis=0)
        return probs

    def distances(self, X, y):
        rng_placeholder = random.split(self.key, X.shape[0])
        out = self.apply_fun(self.params, X, rng_placeholder, False)
        z = out[1]  # (batch,feat,dim)
        z = z[y == 1, ...]
        distances = []
        dist_full = []
        sets = [ set(l) for l in self.cols + [[]]  ]
        for i in range(len(self.cols) + 1):
            euclid = np.sqrt(np.square(z[:, i:i+1, :] - z).sum(-1)).mean(0)  # (feat)
            dist_full.append(euclid)
            idx_noise = np.array([ True if (
                (len(s) > len(sets[i])) and (len(s - sets[i]) == 1) and (2 in s - sets[i])
                ) else False for s in sets ])
            idx_signal = np.array([ True if (
                (len(s) > len(sets[i])) and (len(s - sets[i]) == 1) and (3 in s - sets[i])
                ) else False for s in sets ])
            distances.append(
                np.array([euclid[idx_noise].mean(),  euclid[idx_signal].mean()])
            )

        distances = pd.DataFrame(
            np.stack(distances, axis=0),
            columns=["+noise", "+signal"],
            index=[str(s) for s in sets[:-1]] + ["{}"],
            )  # (feat, 2)
        dist_full = pd.DataFrame(
            np.stack(dist_full, axis=0),
            columns=[str(s) for s in sets[:-1]] + ["{}"],
            index=[str(s) for s in sets[:-1]] + ["{}"],
            )  # (feat, 2)
        return distances, dist_full


class NeuralNet:
    def __init__(
        self,
        model_kwargs,
        training_kwargs,
        loss_fun,
        rng_key=42,
        feat_names=None,
        posterior_params=None,
        ):
        
        """
        model_kwargs: dict, dict(
                        features=features,
                        hidden_size=64,
                        hidden_layers=5,
                        out_size=1,
                        W_init = glorot_normal(),
                        b_init = zeros,
                        eps = 1e-7,
                    )
        training_kwargs: dict, dict(
                        batch_size=32,
                        epochs=100,
                        lr=1e-3,
                    )
        loss_fun: callable, takes (params, output, labels) 
                            and return (loss: scalar, loss_dict: dict for metric monitoring)
        """

        init_fun, apply_fun = nn(
            **model_kwargs
            )
        key = jax.random.PRNGKey(rng_key)
        key, init_key = random.split(key)
        self.key = key
        self.params = init_fun(init_key)
        self.apply_fun = jax.vmap(apply_fun, in_axes=(None, 0, None), out_axes=(0))
        self.loss_fun = loss_fun
        self.posterior_params = posterior_params
        self.training_kwargs = training_kwargs

    def fit(self, X, y):

        params, history, rng = training_loop(
            X=X,
            y=y,
            model_fun=self.apply_fun,
            params=self.params,
            loss_fun=self.loss_fun,
            metric_fun=self.loss_fun,
            rng=self.key,
            **self.training_kwargs
            )
        self.params = params
        self._history = history
        self.key = rng

    def predict_proba(self, X):
        logits = self.apply_fun(self.params, X, None)
        probs = jnp.mean(jnp.stack(jax.nn.sigmoid(logits), axis=0), axis=0)
        return jnp.squeeze(probs)
    
    def predict(self, X):
        output = self.apply_fun(self.params, X, None)
        output = jnp.mean(jnp.stack(output, axis=0), axis=0)
        return jnp.squeeze(output)
   
    def history(self):
        return self._history

