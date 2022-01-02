from abc import ABC, abstractmethod

from .logging import LogUser


class Agent(LogUser, ABC):
    def __init__(self, env):
        """ Construct agent for environment

        Args:
            env:
        """
        super(Agent, self).__init__()
        self.env = env

    @abstractmethod
    def act_batch_test(self, obs):
        pass

    @abstractmethod
    def act_batch_explore(self, obs, global_steps):
        pass


class OffPolicyAgent(Agent):
    def __init__(self, env):
        super(OffPolicyAgent, self).__init__(env=env)
        self.reset()

    def reset(self):
        self.policy_updates = 0

    def log_tabular(self):
        super(OffPolicyAgent, self).log_tabular()
        self.logger.log_tabular('PolicyUpdates', self.policy_updates)

    def update_target(self):
        pass

    def compute_priority(self, data):
        raise NotImplementedError

    def train_on_batch(self, data, **kwargs):
        raise NotImplementedError

    def reset_optimizer(self):
        pass
