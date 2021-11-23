import torch
import torch.nn as nn
from rlutils.pytorch.distributions import make_independent_normal_from_params, apply_squash_log_prob

from .functional import build_mlp


class SquashedGaussianMLPActor(nn.Module):
    def __init__(self, ob_dim, ac_dim, mlp_hidden, num_layers=3):
        super(SquashedGaussianMLPActor, self).__init__()
        self.net = build_mlp(ob_dim, ac_dim * 2, mlp_hidden, num_layers=num_layers)
        self.ac_dim = ac_dim
        self.pi_dist_layer = lambda param: make_independent_normal_from_params(param,
                                                                               min_log_scale=-10.,
                                                                               max_log_scale=5.)

    def select_action(self, inputs):
        inputs, deterministic = inputs
        params = self.net(inputs)
        pi_distribution = self.pi_dist_layer(params)
        if deterministic:
            pi_action = pi_distribution.mean
        else:
            pi_action = pi_distribution.rsample()
        pi_action_final = torch.tanh(pi_action)
        return pi_action_final

    def compute_pi_distribution(self, inputs):
        return self.pi_dist_layer(self.net(inputs))

    def transform_raw_actions(self, raw_actions):
        return torch.tanh(raw_actions)

    def compute_raw_actions(self, actions):
        EPS = 1e-6
        actions = torch.clip(actions, min=-1. + EPS, max=1. - EPS)
        return torch.atanh(actions)

    def compute_log_prob(self, inputs):
        obs, act = inputs
        params = self.net(inputs)
        pi_distribution = self.pi_dist_layer(params)
        pi_action = self.compute_raw_actions(act)
        logp_pi = pi_distribution.log_prob(pi_action)
        logp_pi = apply_squash_log_prob(logp_pi, pi_action)
        return logp_pi

    def forward(self, inputs):
        inputs, deterministic = inputs
        params = self.net(inputs)
        pi_distribution = self.pi_dist_layer(params)
        if deterministic:
            pi_action = pi_distribution.mean
        else:
            pi_action = pi_distribution.rsample()
        logp_pi = pi_distribution.log_prob(pi_action)
        logp_pi = apply_squash_log_prob(logp_pi, pi_action)
        pi_action_final = torch.tanh(pi_action)
        return pi_action_final, logp_pi, pi_action, pi_distribution
