#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# compute.py
"""
Methods for computing concepts, constellations, and integrated information of
subsystems.
"""

import logging
import functools
from time import time
import numpy as np
import multiprocessing
from scipy.sparse.csgraph import connected_components
from scipy.sparse import csr_matrix

from . import utils, constants, config, memory
from .concept_caching import concept as _concept
from .models import Concept, Cut, BigMip
from .network import Network
from .subsystem import Subsystem


# Create a logger for this module.
log = logging.getLogger(__name__)


def concept(subsystem, mechanism):
    """Return the concept specified by the a mechanism within a subsytem.

    Args:
        subsystem (Subsytem): The context in which the mechanism should be
            considered.
        mechanism (tuple(Node)): The candidate set of nodes.

    Returns:
        ``Concept`` -- The pair of maximally irreducible cause/effect
        repertoires that constitute the concept specified by the given
        mechanism.

    .. note::
        The output can be persistently cached to avoid recomputation. This may
        be enabled in the configuration file---however, it is only available if
        the caching backend is a database (not the filesystem). See the
        documentation for :mod:`pyphi.concept_caching` and
        :mod:`pyphi.constants`.
    """
    start = time()

    def time_annotated(concept):
        concept.time = time() - start
        return concept

    # Pre-checks:
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # If the mechanism is empty, there is no concept.
    if not mechanism:
        return time_annotated(subsystem.null_concept)
    # If any node in the mechanism either has no inputs from the subsystem or
    # has no outputs to the subsystem, then the mechanism is necessarily
    # reducible and cannot be a concept (since removing that node would make no
    # difference to at least one of the MICEs).
    if not (subsystem._all_connect_to_any(mechanism, subsystem.nodes) and
            subsystem._any_connect_to_all(subsystem.nodes, mechanism)):
        return time_annotated(
            Concept(mechanism=mechanism, phi=0.0, cause=None, effect=None,
                    subsystem=subsystem))
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    # Passed prechecks; pass it over to the concept caching logic if enabled.
    # Concept caching is only available if the caching backend is a database.
    if (config.CACHE_CONCEPTS and
            config.CACHING_BACKEND == constants.DATABASE):
        return time_annotated(_concept(subsystem, mechanism))
    else:
        return time_annotated(subsystem.concept(mechanism))


def constellation(subsystem):
    """Return the conceptual structure of this subsystem.

    Args:
        subsystem (Subsytem): The subsystem for which to determine the
            constellation.

    Returns:
        ``tuple(Concept)`` -- A tuple of all the Concepts in the constellation.
    """
    concepts = [concept(subsystem, mechanism) for mechanism in
                utils.powerset(subsystem.nodes)]
    # Filter out falsy concepts, i.e. those with effectively zero Phi.
    return tuple(filter(None, concepts))


def concept_distance(c1, c2):
    """Return the distance between two concepts in concept-space.

    Args:
        c1 (Mice): The first concept.
        c2 (Mice): The second concept.

    Returns:
        ``float`` -- The distance between the two concepts in concept-space.
    """
    # Calculate the sum of the past and future EMDs, expanding the repertoires
    # to the full state-space of the subsystem, so that the EMD signatures are
    # the same size.
    return sum([
        utils.hamming_emd(c1.expand_cause_repertoire(),
                          c2.expand_cause_repertoire()),
        utils.hamming_emd(c1.expand_effect_repertoire(),
                          c2.expand_effect_repertoire())])


def _constellation_distance_simple(C1, C2, subsystem):
    """Return the distance between two constellations in concept-space,
    assuming the only difference between them is that some concepts have
    disappeared."""
    # Make C1 refer to the bigger constellation
    if len(C2) > len(C1):
        C1, C2 = C2, C1
    destroyed = [c1 for c1 in C1 if not any(c1.emd_eq(c2) for c2 in C2)]
    return sum(c.phi * concept_distance(c, subsystem.null_concept)
               for c in destroyed)


def _constellation_distance_emd(unique_C1, unique_C2, subsystem):
    """Return the distance between two constellations in concept-space,
    using the generalized EMD."""
    # We need the null concept to be the partitioned constellation, in case a
    # concept is destroyed by a cut (and needs to be moved to the null
    # concept).
    unique_C2 = unique_C2 + [subsystem.null_concept]
    # Get the concept distances from the concepts in the unpartitioned
    # constellation to the partitioned constellation.
    distances = np.array([
        [concept_distance(i, j) for j in unique_C2]
        for i in unique_C1
    ])
    # Now we make the distance matrix.
    # It has blocks of zeros in the upper left and bottom right to make the
    # distance matrix square, and to ensure that we're only moving mass from
    # the unpartitioned constellation to the partitioned constellation.
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    N, M = len(unique_C1), len(unique_C2)
    distance_matrix = np.zeros([N + M] * 2)
    # Top-right block.
    distance_matrix[:N, N:] = distances
    # Bottom-left block.
    distance_matrix[N:, :N] = distances.T
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # Construct the two phi distributions.
    d1 = [c.phi for c in unique_C1] + [0] * M
    d2 = [0] * N + [c.phi for c in unique_C2]
    # Calculate how much phi disappeared and assign it to the null concept (the
    # null concept is the last element in the second distribution).
    d2[-1] = sum(d1) - sum(d2)
    # The sum of the two signatures should be the same.
    assert utils.phi_eq(sum(d1), sum(d2))
    # Calculate!
    return utils.emd(np.array(d1), np.array(d2), distance_matrix)


def constellation_distance(C1, C2, subsystem):
    """Return the distance between two constellations in concept-space.

    Args:
        C1 (tuple(Concept)): The first constellation.
        C2 (tuple(Concept)): The second constellation.
        null_concept (Concept): The null concept of a candidate set, *i.e* the
            "origin" of the concept space in which the given constellations
            reside.

    Returns:
        ``float`` -- The distance between the two constellations in
        concept-space.
    """
    concepts_only_in_C1 = [
        c1 for c1 in C1 if not any(c1.emd_eq(c2) for c2 in C2)]
    concepts_only_in_C2 = [
        c2 for c2 in C2 if not any(c2.emd_eq(c1) for c1 in C1)]
    # If the only difference in the constellations is that some concepts
    # disappeared, then we don't need to use the EMD.
    if not concepts_only_in_C1 or not concepts_only_in_C2:
        return _constellation_distance_simple(C1, C2, subsystem)
    else:
        return _constellation_distance_emd(concepts_only_in_C1,
                                           concepts_only_in_C2,
                                           subsystem)


def conceptual_information(subsystem):
    """Return the conceptual information for a subsystem.

    This is the distance from the subsystem's constellation to the null
    concept."""
    return constellation_distance(constellation(subsystem), (), subsystem)


# TODO document
def _null_mip(subsystem):
    """Returns a BigMip with zero Phi and empty constellations.

    This is the MIP associated with a reducible subsystem."""
    return BigMip(subsystem=subsystem, cut_subsystem=subsystem,
                  phi=0.0,
                  unpartitioned_constellation=(), partitioned_constellation=())


def _single_node_mip(subsystem):
    """Returns a the BigMip of a single-node with a selfloop.

    Whether these have a nonzero |Phi| value depends on the PyPhi constants."""
    if config.SINGLE_NODES_WITH_SELFLOOPS_HAVE_PHI:
        # TODO return the actual concept
        return BigMip(
            phi=0.5,
            unpartitioned_constellation=(),
            partitioned_constellation=(),
            subsystem=subsystem,
            cut_subsystem=subsystem)
    else:
        return _null_mip(subsystem)


def _evaluate_partition(uncut_subsystem, partition,
                        unpartitioned_constellation):
    log.debug("Evaluating partition {}...".format(partition))
    # Compute forward mip.
    forward_cut = Cut(partition[0], partition[1])
    forward_cut_subsystem = Subsystem(uncut_subsystem.node_indices,
                                      uncut_subsystem.network,
                                      cut=forward_cut,
                                      mice_cache=uncut_subsystem._mice_cache)
    forward_constellation = constellation(forward_cut_subsystem)
    forward_mip = BigMip(
        phi=constellation_distance(unpartitioned_constellation,
                                   forward_constellation,
                                   uncut_subsystem),
        unpartitioned_constellation=unpartitioned_constellation,
        partitioned_constellation=forward_constellation,
        subsystem=uncut_subsystem,
        cut_subsystem=forward_cut_subsystem)
    # Short-circuit if the forward MIP has no Phi.
    if utils.phi_eq(forward_mip.phi, 0):
        return forward_mip
    # Compute backward mip.
    backward_cut = Cut(partition[1], partition[0])
    backward_cut_subsystem = Subsystem(uncut_subsystem.node_indices,
                                       uncut_subsystem.network,
                                       cut=backward_cut,
                                       mice_cache=uncut_subsystem._mice_cache)
    backward_constellation = constellation(backward_cut_subsystem)
    backward_mip = BigMip(
        phi=constellation_distance(unpartitioned_constellation,
                                   backward_constellation,
                                   uncut_subsystem),
        unpartitioned_constellation=unpartitioned_constellation,
        partitioned_constellation=backward_constellation,
        subsystem=uncut_subsystem,
        cut_subsystem=backward_cut_subsystem)

    log.debug("Finished evaluating partition {}.".format(partition))
    # Choose minimal unidirectional cut.
    return min(forward_mip, backward_mip)


# Wrapper for _evaluate_partition for parallel processing.
def _eval_wrapper(in_queue, out_queue, subsystem, unpartitioned_constellation):
    while True:
        partition = in_queue.get()
        if partition is None:
            break
        new_mip = _evaluate_partition(subsystem, partition,
                                      unpartitioned_constellation)
        out_queue.put(new_mip)
    out_queue.put(None)


def _find_mip_parallel(subsystem, bipartitions, unpartitioned_constellation,
                       min_mip):
    """Parallel loop over all partitions, using the specified number of
    cores."""
    if config.NUMBER_OF_CORES < 0:
        number_of_processes = (multiprocessing.cpu_count() +
                               config.NUMBER_OF_CORES + 1)
    elif config.NUMBER_OF_CORES <= multiprocessing.cpu_count():
        number_of_processes = config.NUMBER_OF_CORES
    else:
        raise ValueError(
            'Invalid number of cores; value may not be 0, and must be less'
            'than the number of cores ({} for this '
            'system).'.format(multiprocessing.cpu_count()))
    # Define input and output queues to allow short-circuit if a cut if found
    # with zero Phi. Load the input queue with all possible cuts and a 'poison
    # pill' for each process.
    in_queue = multiprocessing.Queue()
    out_queue = multiprocessing.Queue()
    for partition in bipartitions:
        in_queue.put(partition)
    for i in range(number_of_processes):
        in_queue.put(None)
    # Initialize the processes and start them.
    processes = [
        multiprocessing.Process(target=_eval_wrapper,
                                args=(in_queue, out_queue, subsystem,
                                      unpartitioned_constellation))
        for i in range(number_of_processes)
    ]
    for i in range(number_of_processes):
        processes[i].start()
    # Continue to process output queue until all processes have completed, or a
    # 'poison pill' has been returned.
    while True:
        new_mip = out_queue.get()
        if new_mip is None:
            number_of_processes -= 1
            if number_of_processes == 0:
                break
        elif utils.phi_eq(new_mip.phi, 0):
            min_mip = new_mip
            for process in processes:
                process.terminate()
            break
        else:
            if new_mip < min_mip:
                min_mip = new_mip
    return min_mip


def _find_mip_sequential(subsystem, bipartitions, unpartitioned_constellation,
                         min_mip):
    """Sequentially loop over all partitions, holding only two BigMips in
    memory at once."""
    for i, partition in enumerate(bipartitions):
        new_mip = _evaluate_partition(
            subsystem, partition, unpartitioned_constellation)
        log.debug("Finished {} of {} partitions.".format(
            i + 1, len(bipartitions)))
        if new_mip < min_mip:
            min_mip = new_mip
        # Short-circuit as soon as we find a MIP with effectively 0 phi.
        if not min_mip:
            break
    return min_mip


# TODO document big_mip
@memory.cache(ignore=["subsystem"])
def _big_mip(cache_key, subsystem):
    log.info("Calculating big-phi data for {}...".format(subsystem))
    start = time()

    # Annote a BigMip with the total elapsed calculation time, and optionally
    # also with the time taken to calculate the unpartitioned constellation.
    def time_annotated(big_mip, small_phi_time=0.0):
        big_mip.time = time() - start
        big_mip.small_phi_time = small_phi_time
        return big_mip

    # Special case for single-node subsystems.
    if len(subsystem) == 1:
        log.info('Single-node {}; returning the hard-coded single-node MIP '
                 'immediately.'.format(subsystem))
        return time_annotated(_single_node_mip(subsystem))

    # Check for degenerate cases
    # =========================================================================
    # Phi is necessarily zero if the subsystem is:
    #   - not strongly connected;
    #   - empty; or
    #   - an elementary mechanism (i.e. no nontrivial bipartitions).
    # So in those cases we immediately return a null MIP.
    if not subsystem:
        log.info('Subsystem {} is empty; returning null MIP '
                 'immediately.'.format(subsystem))
        return time_annotated(_null_mip(subsystem))
    # Get the connectivity of just the subsystem nodes.
    submatrix_indices = np.ix_(subsystem.node_indices, subsystem.node_indices)
    cm = subsystem.network.connectivity_matrix[submatrix_indices]
    # Get the number of strongly connected components.
    num_components, _ = connected_components(csr_matrix(cm),
                                             connection='strong')
    if num_components > 1:
        log.info('{} is not strongly connected; returning null MIP '
                 'immediately.'.format(subsystem))
        return time_annotated(_null_mip(subsystem))
    # =========================================================================

    # The first bipartition is the null cut (trivial bipartition), so skip it.
    # TODO!!! see if we can check if any of these cuts will actually do
    # anything before evaluating them
    bipartitions = utils.bipartition(subsystem.node_indices)[1:]

    log.debug("Finding unpartitioned constellation...")
    small_phi_start = time()
    unpartitioned_constellation = constellation(subsystem)
    small_phi_time = time() - small_phi_start
    log.debug("Found unpartitioned constellation.")
    min_mip = _null_mip(subsystem)
    min_mip.phi = float('inf')
    if config.PARALLEL_CUT_EVALUATION:
        min_mip = _find_mip_parallel(subsystem, bipartitions,
                                     unpartitioned_constellation, min_mip)
    else:
        min_mip = _find_mip_sequential(subsystem, bipartitions,
                                       unpartitioned_constellation, min_mip)
    result = time_annotated(min_mip, small_phi_time)

    log.info("Finished calculating big-phi data for {}.".format(subsystem))
    log.debug("RESULT: \n" + str(result))

    return result


# Wrapper to ensure that the cache key is the native hash of the subsystem, so
# joblib doesn't mistakenly recompute things when the subsystem's MICE cache is
# changed.
@functools.wraps(_big_mip)
def big_mip(subsystem):
    """Return the MIP of a subsystem.

    Args:
        subsystem (Subsystem): The candidate set of nodes.

    Returns:
        ``BigMip`` -- A nested structure containing all the data from the
        intermediate calculations. The top level contains the basic MIP
        information for the given subsystem. See :class:`models.BigMip`.
    """
    return _big_mip(hash(subsystem), subsystem)


def big_phi(subsystem):
    """Return the |big_phi| value of a subsystem."""
    return big_mip(subsystem).phi


def possible_main_complexes(network):
    """"Return a generator of the subsystems of a network that could be a main
    complex.

    This is the just powerset of the nodes that have at least one input and
    output (nodes with no inputs or no outputs cannot be part of a main
    complex, because they do not have a causal link with the rest of the
    subsystem in the past or future, respectively)."""
    inputs = np.sum(network.connectivity_matrix, 1)
    outputs = np.sum(network.connectivity_matrix, 0)
    nodes_have_inputs_and_outputs = np.logical_and(inputs > 0, outputs > 0)
    causally_significant_nodes = np.where(nodes_have_inputs_and_outputs)[0]
    for subset in utils.powerset(causally_significant_nodes):
        yield Subsystem(subset, network)


def subsystems(network):
    """Return a generator of all possible subsystems of a network."""
    for subset in utils.powerset(network.node_indices):
        yield Subsystem(subset, network)


def complexes(network):
    """Return a generator for all irreducible complexes of the network."""
    if not isinstance(network, Network):
        raise ValueError(
            """Input must be a Network (perhaps you passed a Subsystem
            instead?)""")
    return tuple(filter(None, (big_mip(subsystem) for subsystem in
            possible_main_complexes(network))))


def all_complexes(network):
    """Return a generator for all complexes of the network, including
    reducible, zero-phi complexes (which are not, strictly speaking, complexes
    at all)."""
    if not isinstance(network, Network):
        raise ValueError(
            """Input must be a Network (perhaps you passed a Subsystem
            instead?)""")
    return (big_mip(subsystem) for subsystem in subsystems(network))


def main_complex(network):
    """Return the main complex of the network."""
    if not isinstance(network, Network):
        raise ValueError(
            """Input must be a Network (perhaps you passed a Subsystem
            instead?)""")
    log.info("Calculating main complex for " + str(network) + "...")
    result = max(complexes(network))
    log.info("Finished calculating main complex for" + str(network) + ".")
    log.debug("RESULT: \n" + str(result))
    return result
