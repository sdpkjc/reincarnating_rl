# coding=utf-8
# Copyright 2022 The Reincarnating RL Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Networks for Jax Persistence agents."""

import collections
import time
from typing import Tuple

from dopamine.discrete_domains import atari_lib
from dopamine.jax import networks
from flax import linen as nn
import gin
import jax
import jax.numpy as jnp
import ml_collections
import numpy as onp
from reincarnating_rl import impala_networks


NetworkType = collections.namedtuple('dqn_network', ['q_values'])
FullRainbowNetwork = networks.FullRainbowNetwork
ImplicitQuantileNetwork = networks.ImplicitQuantileNetwork


def get_atari_b14_config():
  """Returns the ViT-B/16 configuration except using patch size of 14x14."""
  config = ml_collections.ConfigDict()
  config.name = 'ViT-B_14'
  config.patches = ml_collections.ConfigDict({'size': (14, 14)})
  config.hidden_size = 768
  config.transformer = ml_collections.ConfigDict()
  config.transformer.mlp_dim = 3072
  config.transformer.num_heads = 12
  config.transformer.num_layers = 12
  config.transformer.attention_dropout_rate = 0.0
  config.transformer.dropout_rate = 0.0
  config.classifier = 'token'
  config.representation_size = None
  return config


def preprocess_atari_inputs(x):
  """Input normalization for Atari 2600 input frames."""
  return x.astype(jnp.float32) / 255.


### DQN Networks ###
@gin.configurable
class NatureResidualDQNNetwork(nn.Module):
  """CNN used to compute the agent's Q-values and residual weights."""
  num_actions: int
  inputs_preprocessed: bool = False

  @nn.compact
  def __call__(self, x):
    initializer = nn.initializers.xavier_uniform()
    if not self.inputs_preprocessed:
      x = preprocess_atari_inputs(x)
    x = nn.Conv(features=32, kernel_size=(8, 8), strides=(4, 4),
                kernel_init=initializer)(x)
    x = nn.relu(x)
    x = nn.Conv(features=64, kernel_size=(4, 4), strides=(2, 2),
                kernel_init=initializer)(x)
    x = nn.relu(x)
    x = nn.Conv(features=64, kernel_size=(3, 3), strides=(1, 1),
                kernel_init=initializer)(x)
    x = nn.relu(x)
    x = x.reshape((-1))  # flatten
    x = nn.Dense(features=512, kernel_init=initializer)(x)
    # Zero initialization of residual values -- don't corrupt initial teacher-Q
    # values with randomly initialized Q-values.
    alpha = self.param('alpha', nn.initializers.zeros, self.num_actions)
    x = nn.relu(x)
    q_values = nn.Dense(features=self.num_actions,
                        kernel_init=initializer)(x)
    return NetworkType(q_values * alpha)


@gin.configurable
class ImpalaEncoder(nn.Module):
  """Impala Network which also outputs penultimate representation layers."""
  nn_scale: int = 1
  stack_sizes: Tuple[int, ...] = (16, 32, 32)
  num_blocks: int = 2

  @nn.compact
  def __call__(self, x):
    conv_out = x

    for stack_size in self.stack_sizes:
      conv_out = impala_networks.Stack(
          num_ch=stack_size * self.nn_scale, num_blocks=self.num_blocks)(
              conv_out)

    conv_out = nn.relu(conv_out)
    return conv_out


@gin.configurable
class ImpalaRainbowNetwork(nn.Module):
  """Jax Rainbow network for Full Rainbow, using Impala as its encoder.

  Attributes:
    num_actions: int, number of actions the agent can take at any state.
    num_atoms: int, the number of buckets of the value function distribution.
    noisy: bool, Whether to use noisy networks.
    dueling: bool, Whether to use dueling network architecture.
    distributional: bool, whether to use distributional RL.
  """
  num_actions: int
  num_atoms: int
  noisy: bool = True
  dueling: bool = True
  distributional: bool = True
  inputs_preprocessed: bool = False

  def setup(self):
    self.encoder = ImpalaEncoder()

  @nn.compact
  def __call__(self, x, support, eval_mode=False, key=None):
    # Generate a random number generation key if not provided
    if key is None:
      key = jax.random.PRNGKey(int(time.time() * 1e6))

    if not self.inputs_preprocessed:
      x = preprocess_atari_inputs(x)

    x = self.encoder(x)
    x = x.reshape((-1))  # flatten

    net = networks.feature_layer(key, self.noisy, eval_mode=eval_mode)
    x = net(x, features=512)  # Single hidden layer of size 512
    x = nn.relu(x)

    if self.dueling:
      adv = net(x, features=self.num_actions * self.num_atoms)
      value = net(x, features=self.num_atoms)
      adv = adv.reshape((self.num_actions, self.num_atoms))
      value = value.reshape((1, self.num_atoms))
      logits = value + (adv - (jnp.mean(adv, axis=0, keepdims=True)))
    else:
      x = net(x, features=self.num_actions * self.num_atoms)
      logits = x.reshape((self.num_actions, self.num_atoms))

    if self.distributional:
      probabilities = nn.softmax(logits)
      q_values = jnp.sum(support * probabilities, axis=1)
      return atari_lib.RainbowNetworkType(q_values, logits, probabilities)
    q_values = jnp.sum(logits, axis=1)  # Sum over all the num_atoms
    return atari_lib.DQNNetworkType(q_values)


@gin.configurable
class ImpalaImplicitQuantileNetwork(nn.Module):
  """IQN Network with Impala Encoder."""
  num_actions: int
  quantile_embedding_dim: int
  inputs_preprocessed: bool = False

  def setup(self):
    self.encoder = ImpalaEncoder()

  @nn.compact
  def __call__(self, x, num_quantiles, rng):
    initializer = nn.initializers.variance_scaling(
        scale=1.0 / jnp.sqrt(3.0),
        mode='fan_in',
        distribution='uniform')

    if not self.inputs_preprocessed:
      x = preprocess_atari_inputs(x)

    x = self.encoder(x)
    x = x.reshape((-1))  # flatten
    state_vector_length = x.shape[-1]
    state_net_tiled = jnp.tile(x, [num_quantiles, 1])
    quantiles_shape = [num_quantiles, 1]
    quantiles = jax.random.uniform(rng, shape=quantiles_shape)
    quantile_net = jnp.tile(quantiles, [1, self.quantile_embedding_dim])
    quantile_net = (
        jnp.arange(1, self.quantile_embedding_dim + 1, 1).astype(jnp.float32)
        * onp.pi
        * quantile_net)
    quantile_net = jnp.cos(quantile_net)
    quantile_net = nn.Dense(features=state_vector_length,
                            kernel_init=initializer)(quantile_net)
    quantile_net = nn.relu(quantile_net)
    x = state_net_tiled * quantile_net
    x = nn.Dense(features=512, kernel_init=initializer)(x)
    x = nn.relu(x)
    quantile_values = nn.Dense(features=self.num_actions,
                               kernel_init=initializer)(x)
    return atari_lib.ImplicitQuantileNetworkType(quantile_values, quantiles)
