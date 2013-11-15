#!/usr/bin/env python
# This file is part of the dune-pymor project:
#   https://github.com/pyMor/dune-pymor
# Copyright Holders: Felix Albrecht, Stephan Rave
# License: BSD 2-Clause License (http://opensource.org/licenses/BSD-2-Clause)

'''Dune LRBMS demo.

Usage:
  multiscale-generic-sipdg_demo.py SETTINGSFILE

Arguments:
  SETTINGSFILE File that can be understood by pythons ConfigParser and by dunes ParameterTree
'''

from __future__ import absolute_import, division, print_function

config_defaults = {'framework': 'rb',
                   'num_training_samples': '100',
                   'training_set': 'random',
                   'reductor': 'generic',
                   'extension_algorithm': 'gram_schmidt',
                   'extension_algorithm_product': 'h1',
                   'greedy_error_norm': 'h1',
                   'use_estimator': 'False',
                   'max_rb_size': '100',
                   'target_error': '0.01',
                   'num_test_samples': '100',
                   'test_set': 'training',
                   'test_error_norm': 'h1'}

import sys
import math as m
import time
from functools import partial
from itertools import izip
import numpy as np
from docopt import docopt
from scipy.sparse import coo_matrix
from scipy.sparse import bmat as sbmat
from numpy import bmat as nbmat
import ConfigParser

import linearellipticmultiscaleexample as dune_module
from dune.pymor.core import wrap_module

import pymor.core as core
core.logger.MAX_HIERACHY_LEVEL = 2
from pymor.parameters import CubicParameterSpace
from pymor.core.exceptions import ConfigError
from pymor.core import cache
from pymor.reductors import reduce_generic_rb
from pymor.reductors.basic import GenericRBReconstructor, reduce_generic_rb
from pymor.reductors.linear import reduce_stationary_affine_linear
from pymor.algorithms import greedy, gram_schmidt_basis_extension, pod_basis_extension
from pymor.algorithms.basisextension import block_basis_extension
from pymor.operators import NumpyMatrixOperator
from pymor.operators.basic import NumpyLincombMatrixOperator
from pymor.operators.block import BlockOperator
from pymor.la import NumpyVectorArray
from pymor.la.basic import induced_norm
from pymor.la.blockvectorarray import BlockVectorArray
from pymor.discretizations import StationaryDiscretization
from pymor import defaults

logger = core.getLogger('pymor.main.demo')
logger.setLevel('INFO')
core.getLogger('pymor.WrappedDiscretization').setLevel('WARN')
core.getLogger('pymor.algorithms').setLevel('INFO')
core.getLogger('dune.pymor.discretizations').setLevel('WARN')

def load_dune_module(settings_filename):

    logger.info('initializing dune module...')
    #example = dune_module.LinearellipticMultiscaleExample__DuneALUConformGrid__lt___2__2___gt__()
    example = dune_module.LinearellipticMultiscaleExample__DuneSGrid__lt___2__2___gt__()
    example.initialize([settings_filename])
    _, wrapper = wrap_module(dune_module)
    return example, wrapper


def perform_standard_rb(config, detailed_discretization, training_samples):

    # parse config
    reductor_id = config.get('pymor', 'reductor')
    if reductor_id == 'generic':
        reductor = reduce_generic_rb,
    elif reductor_id == 'stationary_affine_linear':
        reductor_error_product = config.get('pymor', 'reductor_error_product')
        assert reductor_error_product == 'None'
        reductor_error_product = None
        reductor = partial(reduce_stationary_affine_linear, error_product=reductor_error_product)
    else:
        raise ConfigError('unknown \'pymor.reductor\' given: \'{}\''.format(reductor_id))

    extension_algorithm_product_id = config.get('pymor', 'extension_algorithm_product')
    assert extension_algorithm_product_id == 'h1'
    extension_algorithm_product = detailed_discretization.h1_product

    extension_algorithm_id = config.get('pymor', 'extension_algorithm')
    if extension_algorithm_id == 'gram_schmidt':
        extension_algorithm = gram_schmidt_basis_extension
    elif extension_algorithm_id == 'pod':
        extension_algorithm = pod_basis_extension
    else:
        raise ConfigError('unknown \'pymor.extension_algorithm\' given: \'{}\''.format(extension_algorithm_id))

    extension_algorithm = partial(extension_algorithm, product=extension_algorithm_product)

    greedy_error_norm_id = config.get('pymor', 'greedy_error_norm')
    assert greedy_error_norm_id == 'h1'
    greedy_error_norm = detailed_discretization.h1_norm

    greedy_use_estimator = config.getboolean('pymor', 'use_estimator')
    greedy_max_rb_size = config.getint('pymor', 'max_rb_size')
    greedy_target_error = config.getfloat('pymor', 'target_error')

    # do the actual work
    greedy_data = greedy(detailed_discretization,
                         reduce_generic_rb,
                         training_samples,
                         initial_data=detailed_discretization.functionals['rhs'].type_source.empty(
                                      dim=detailed_discretization.functionals['rhs'].dim_source),
                         use_estimator=greedy_use_estimator,
                         error_norm=greedy_error_norm,
                         extension_algorithm=extension_algorithm,
                         max_extensions=greedy_max_rb_size,
                         target_error=greedy_target_error)
    rb_size = len(greedy_data['data'])

    #report
    report_string = '''
Greedy basis generation:
    used estimator:        {greedy_use_estimator}
    error norm:            {greedy_error_norm_id}
    extension method:      {extension_algorithm_id} ({extension_algorithm_product_id})
    prescribed basis size: {greedy_max_rb_size}
    prescribed error:      {greedy_target_error}
    actual basis size:     {rb_size}
    elapsed time:          {greedy_data[time]}
'''.format(**locals())

    return report_string, greedy_data


def perform_lrbms(config, multiscale_discretization, training_samples):

    num_subdomains = multiscale_discretization._impl.num_subdomains()

    # parse config
    extension_algorithm_product_id = config.get('pymor', 'extension_algorithm_product')
    assert extension_algorithm_product_id == 'h1'

    extension_algorithm_id = config.get('pymor', 'extension_algorithm')
    if extension_algorithm_id == 'gram_schmidt':
        extension_algorithm = gram_schmidt_basis_extension
    elif extension_algorithm_id == 'pod':
        extension_algorithm = pod_basis_extension
    else:
        raise ConfigError('unknown \'pymor.extension_algorithm\' given:\'{}\''.format(extension_algorithm_id))

    extension_algorithm = partial(block_basis_extension,
                                  extension_algorithm=[partial(extension_algorithm,
                                                               product=multiscale_discretization.local_product(ss, extension_algorithm_product_id))
                                                       for ss in np.arange(num_subdomains)])

    greedy_error_norm_id = config.get('pymor', 'greedy_error_norm')
    assert greedy_error_norm_id == 'h1'
    greedy_error_norm = multiscale_discretization.h1_norm

    greedy_use_estimator = config.getboolean('pymor', 'use_estimator')
    assert greedy_use_estimator is False
    greedy_max_rb_size = config.getint('pymor', 'max_rb_size')
    greedy_target_error = config.getfloat('pymor', 'target_error')

    # do the actual work
    greedy_data = greedy(multiscale_discretization,
                         reduce_generic_rb,
                         training_samples,
                         initial_data=[multiscale_discretization.local_rhs(ss).type_source.empty(dim=multiscale_discretization.local_rhs(ss).dim_source)
                                       for ss in np.arange(num_subdomains)],
                         use_estimator=greedy_use_estimator,
                         error_norm=greedy_error_norm,
                         extension_algorithm=extension_algorithm,
                         max_extensions=greedy_max_rb_size,
                         target_error=greedy_target_error)

    rb_size = [len(local_data) for local_data in greedy_data['data']]

    #report
    report_string = '''
Greedy basis generation:
    used estimator:        {greedy_use_estimator}
    error norm:            {greedy_error_norm_id}
    extension method:      {extension_algorithm_id} ({extension_algorithm_product_id})
    prescribed basis size: {greedy_max_rb_size}
    prescribed error:      {greedy_target_error}
    actual basis size:     {rb_size}
    elapsed time:          {greedy_data[time]}
'''.format(**locals())

    return report_string, greedy_data


def test_quality(config, test_samples, detailed_discretization, greedy_data, strategy = 'stochastic'):

    # parse config
    test_error_norm = config.get('pymor', 'test_error_norm')
    assert test_error_norm == 'h1'
    test_error_norm = detailed_discretization.h1_norm

    # get reduced quantities
    reduced_discretization = greedy_data['reduced_discretization']
    reconstructor          = greedy_data['reconstructor']

    # run the test
    test_size = len(test_samples)
    tic = time.time()
    err_max = -1
    for mu in test_samples:
        detailed_solution = detailed_discretization.solve(mu)
        reduced_DoF_vector = reduced_discretization.solve(mu)
        reduced_solution = reconstructor.reconstruct(reduced_DoF_vector)
        err = test_error_norm(detailed_solution - reduced_solution)
        if err > err_max:
            err_max = err
            mumax = mu
    toc = time.time()
    t_est = toc - tic

    # and report
    return '''
{strategy} error estimation:
    number of samples:     {test_size}
    maximal error:         {err_max}  (for mu = {mumax})
    elapsed time:          {t_est}
'''.format(**locals())


if __name__ == '__main__':
    # first of all, clear the cache
    cache.clear_caches()
    # parse arguments
    args = docopt(__doc__)
    config = ConfigParser.ConfigParser(config_defaults)
    try:
        config.readfp(open(args['SETTINGSFILE']))
        assert config.has_section('pymor')
    except:
        raise ConfigError('SETTINGSFILE has to be the name of an existing file that contains a [pymor] section')
    if config.has_section('pymor.defaults'):
        float_suffixes = ['_tol', '_threshold']
        boolean_suffixes = ['_find_duplicates', '_check', '_symmetrize', '_orthonormalize', '_raise_negative',
                            'compact_print']
        int_suffixes = ['_maxiter']
        for key, value in config.items('pymor.defaults'):
            if any([len(key) >= len(suffix) and key[-len(suffix):] == suffix for suffix in float_suffixes]):
                defaults.__setattr__(key, config.getfloat('pymor.defaults', key))
            elif any([len(key) >= len(suffix) and key[-len(suffix):] == suffix for suffix in boolean_suffixes]):
                defaults.__setattr__(key, config.getboolean('pymor.defaults', key))
            elif any([len(key) >= len(suffix) and key[-len(suffix):] == suffix for suffix in int_suffixes]):
                defaults.__setattr__(key, config.getint('pymor.defaults', key))

    # load module
    example, wrapper = load_dune_module(args['SETTINGSFILE'])

    # create global cg discretization
    global_cg_discretization = wrapper[example.global_discretization()]
    global_cg_discretization = global_cg_discretization.with_(
        parameter_space=CubicParameterSpace(global_cg_discretization.parameter_type, 0.1, 10.0))
    logger.info('the parameter type is {}'.format(global_cg_discretization.parameter_type))
    # create multiscale discretization
    multiscale_discretization = wrapper[example.multiscale_discretization()]
    multiscale_discretization = multiscale_discretization.with_(
        parameter_space=global_cg_discretization.parameter_space)

    # create training set
    num_training_samples = config.getint('pymor', 'num_training_samples')
    training_set_sampling_strategy = config.get('pymor', 'training_set')
    if training_set_sampling_strategy == 'random':
        training_samples = list(global_cg_discretization.parameter_space.sample_randomly(num_training_samples))
    else:
        raise ConfigError('unknown \'training_set\' sampling strategy given: \'{}\''.format(training_set_sampling_strategy))

    # run the model reduction
    framework = config.get('pymor', 'framework')
    if framework == 'rb':
        logger.info('running standard rb for global cg discretization:')
        detailed_discretization = global_cg_discretization
        reduction_report, data = perform_standard_rb(config, detailed_discretization, training_samples)
    elif framework == 'lrbms':
        logger.info('running lrbms with {} subdomains:'.format(multiscale_discretization._impl.num_subdomains()))
        detailed_discretization = multiscale_discretization
        reduction_report, data = perform_lrbms(config, detailed_discretization, training_samples)
    else:
        raise ConfigError('unknown \'framework\' given: \'{}\''.format(framework))

    # test quality
    num_test_samples = config.getint('pymor', 'num_test_samples')
    test_set_sampling_strategy = config.get('pymor', 'test_set')
    if test_set_sampling_strategy == 'training':
        test_samples = training_samples
    else:
        raise ConfigError('unknown \'test_set\' sampling strategy given: \'{}\''.format(test_set_sampling_strategy))
    test_report = test_quality(config, test_samples, detailed_discretization, data, test_set_sampling_strategy)

    logger.info(reduction_report)
    logger.info(test_report)