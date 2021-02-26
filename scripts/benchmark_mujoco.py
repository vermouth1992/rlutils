"""
Benchmark SAC and TD3 on update_every=1, 50
TD3 output activation=tf.
"""

import unittest

import rlutils.infra as rl_infra


def thunk(**kwargs):
    from rlutils.algos.tf.mf import sac, td3
    temp = [sac, td3]
    algo = kwargs.pop('algo')
    eval(f'{algo}.Runner.main')(**kwargs)


class Benchmark(unittest.TestCase):
    def test_sac_update_every(self):
        algo = 'sac'
        experiments = rl_infra.runner.ExperimentGrid()
        experiments.add(key='algo', vals=algo, in_name=True, shorthand='ALG')
        experiments.add(key='update_every', vals=[1, 50], in_name=True, shorthand='UPDATE')
        experiments.add(key='epochs', vals=300)
        experiments.add(key='seed', vals=list(range(110, 120)))
        experiments.add(key='env_name', vals=['Walker2d-v2', 'Hopper-v2', 'HalfCheetah-v2', 'Ant-v2'], shorthand='ENV')
        experiments.run(thunk=thunk, data_dir='benchmark_results')

    def test_td3_update_every(self):
        algo = 'td3'
        experiments = rl_infra.runner.ExperimentGrid()
        experiments.add(key='algo', vals=algo, in_name=True, shorthand='ALG')
        experiments.add(key='update_every', vals=[1, 50], in_name=True, shorthand='UPDATE')
        experiments.add(key='epochs', vals=300)
        experiments.add(key='seed', vals=list(range(110, 120)))
        experiments.add(key='env_name', vals=['Walker2d-v2', 'Hopper-v2', 'HalfCheetah-v2', 'Ant-v2'], shorthand='ENV')
        experiments.run(thunk=thunk, data_dir='benchmark_results')

    def test_td3_out_activation(self):
        algo = 'td3'
        experiments = rl_infra.runner.ExperimentGrid()
        experiments.add(key='algo', vals=algo, in_name=True, shorthand='ALG')
        experiments.add(key='update_every', vals=1, in_name=True, shorthand='UPDATE')
        experiments.add(key='epochs', vals=300)
        experiments.add(key='seed', vals=list(range(110, 120)))
        experiments.add(key='env_name', vals=['Walker2d-v2', 'Hopper-v2', 'HalfCheetah-v2', 'Ant-v2'], shorthand='ENV')
        experiments.add(key='out_activation', vals='sin', in_name=True)
        experiments.run(thunk=thunk, data_dir='benchmark_results')


if __name__ == '__main__':
    unittest.main()