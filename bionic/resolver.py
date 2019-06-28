'''
Contains the core logic for resolving Entities by executing Tasks.
'''
from __future__ import absolute_import

from builtins import object
from .datatypes import Provenance, Query, Result, ResultGroup
from .exception import UndefinedEntityError

import logging
# TODO At some point it might be good to have the option of Bionic handling its
# own logging.  Probably it would manage its own logger instances and inject
# them into tasks, while providing the option of either handling the output
# itself or routing it back to the global logging system.
logger = logging.getLogger(__name__)


class EntityResolver(object):
    # --- Public API.

    def __init__(self, flow_state):
        self._flow_state = flow_state

        # This state is needed to do any resolution at all.  Once it's
        # initialized, we can use it to bootstrap the requirements for "full"
        # resolution below.
        self._is_ready_for_bootstrap_resolution = False
        self._task_lists_by_entity_name = None
        self._task_states_by_key = None

        # This state allows us to do full resolution for external callers.
        self._is_ready_for_full_resolution = False
        self._persistent_cache = None

    def get_ready(self):
        """
        Make sure this Resolver is ready to resolve().  Calling this is not
        necessary but allows errors to surface earlier.
        """
        self._get_ready_for_full_resolution()

    def resolve(self, entity_name):
        """
        Given an entity name, computes and returns a ResultGroup containing
        all values for that entity.
        """
        self.get_ready()
        return self._compute_result_group_for_entity_name(entity_name)

    def export_dag(self, include_core=False):
        '''
        Constructs a NetworkX graph corresponding to the DAG of tasks.  There
        is one node per task key -- i.e., for each artifact that can be created
        (uniquely defined by an entity name and a case key); and one edge from
        each task key to each key that depends on it.  Each node is represented
        by a TaskKey, and also has the following attributes:

            name: a short, unique, human-readable identifier
            entity_name: the name of the entity for this task key
            case_key: the case key for this task key
            task_ix: the task key's index in the ordered series of case keys
                     for its entity
        '''
        import networkx as nx

        def should_include_entity_name(name):
            return include_core or not self.entity_is_internal(entity_name)

        self.get_ready()

        graph = nx.DiGraph()

        for entity_name, tasks in (
                self._task_lists_by_entity_name.items()):
            if not should_include_entity_name(entity_name):
                continue

            if len(tasks) == 1:
                name_template = '{entity_name}'
            else:
                name_template = '{entity_name}[{task_ix}]'

            for task_ix, task in enumerate(sorted(
                    tasks, key=lambda task: task.keys[0].case_key)):
                task_key = task.key_for_entity_name(entity_name)
                state = self._task_states_by_key[task_key]

                node_name = name_template.format(
                    entity_name=entity_name, task_ix=task_ix)

                graph.add_node(
                    task_key,
                    name=node_name,
                    entity_name=entity_name,
                    case_key=task_key.case_key,
                    task_ix=task_ix,
                )

                for child_state in state.children:
                    for child_task_key in child_state.task.keys:
                        if not should_include_entity_name(
                                child_task_key.entity_name):
                            continue
                        if task_key not in child_state.task.dep_keys:
                            continue
                        graph.add_edge(task_key, child_task_key)

        return graph

    def entity_is_internal(self, entity_name):
        return entity_name.startswith('core__')

    # --- Private helpers.

    def _get_ready_for_full_resolution(self):
        if self._is_ready_for_full_resolution:
            return

        self._get_ready_for_bootstrap_resolution()

        self._persistent_cache = self._bootstrap_singleton(
            'core__persistent_cache')

        self._is_ready_for_full_resolution = True

    def _get_ready_for_bootstrap_resolution(self):
        if self._is_ready_for_bootstrap_resolution:
            return

        # Generate the static key spaces and tasks for each entity.
        self._key_spaces_by_entity_name = {}
        self._task_lists_by_entity_name = {}
        for name in self._flow_state.providers_by_name.keys():
            self._populate_entity_info(name)

        # Initialize a state object for each task.
        self._task_states_by_key = {}
        for tasks in self._task_lists_by_entity_name.values():
            for task in tasks:
                task_state = TaskState(task)
                for key in task.keys:
                    self._task_states_by_key[key] = task_state

        # Connect the task states to each other in a graph.
        for tasks in self._task_lists_by_entity_name.values():
            for task in tasks:
                task_state = self._task_states_by_key[task.keys[0]]
                for dep_key in task.dep_keys:
                    dep_state = self._task_states_by_key[dep_key]

                    task_state.parents.append(dep_state)
                    dep_state.children.append(task_state)

        self._is_ready_for_bootstrap_resolution = True

    def _populate_entity_info(self, entity_name):
        if entity_name in self._task_lists_by_entity_name:
            return

        provider = self._flow_state.get_provider(entity_name)

        dep_names = provider.get_dependency_names()
        for dep_name in dep_names:
            self._populate_entity_info(dep_name)

        dep_key_spaces_by_name = {
            dep_name: self._key_spaces_by_entity_name[dep_name]
            for dep_name in dep_names
        }

        dep_task_key_lists_by_name = {
            dep_name: [
                task.key_for_entity_name(dep_name)
                for task in self._task_lists_by_entity_name[dep_name]
            ]
            for dep_name in dep_names
        }

        self._key_spaces_by_entity_name[entity_name] =\
            provider.get_key_space(dep_key_spaces_by_name)

        self._task_lists_by_entity_name[entity_name] = provider.get_tasks(
            dep_key_spaces_by_name,
            dep_task_key_lists_by_name)

    def _bootstrap_singleton(self, entity_name):
        result_group = self._compute_result_group_for_entity_name(
            entity_name)
        if len(result_group) == 0:
            raise ValueError(
                "No values were defined for internal bootstrap entity %r" %
                entity_name)
        if len(result_group) > 1:
            values = [result.value for result in result_group]
            raise ValueError(
                "Bootstrap entity %r must have exactly one value; "
                "got %d (%r)" % (entity_name, len(values), values))
        return result_group[0].value

    def _compute_result_group_for_entity_name(self, entity_name):
        tasks = self._task_lists_by_entity_name.get(entity_name)
        if tasks is None:
            raise UndefinedEntityError(
                "Entity %r is not defined" % entity_name)
        requested_task_states = [
            self._task_states_by_key[task.keys[0]]
            for task in tasks
        ]
        ready_task_states = list(requested_task_states)

        blocked_task_key_tuples = set()

        logged_task_keys = set()

        while ready_task_states:
            state = ready_task_states.pop()

            if state.is_complete():
                for task_key in state.task.keys:
                    if task_key not in logged_task_keys:
                        loggable_str = self._loggable_str_for_task_key(
                            task_key)
                        self._log(
                            'Accessed  %s from in-memory cache', loggable_str)
                        logged_task_keys.add(task_key)
                continue

            if not state.is_blocked():
                self._compute_task_state(state)

                for task_key in state.task.keys:
                    logged_task_keys.add(task_key)

                for child_state in state.children:
                    if child_state.task.keys in blocked_task_key_tuples and\
                            not child_state.is_blocked():
                        ready_task_states.append(child_state)
                        blocked_task_key_tuples.remove(child_state.task.keys)

                continue

            for dep_state in state.parents:
                if not dep_state.is_complete():
                    ready_task_states.append(dep_state)
            blocked_task_key_tuples.add(state.task.keys)

        assert len(blocked_task_key_tuples) == 0, blocked_task_key_tuples
        for state in requested_task_states:
            assert state.is_complete(), state

        return ResultGroup(
            results=[
                state.results_by_name[entity_name]
                for state in requested_task_states
            ],
            key_space=self._key_spaces_by_entity_name[entity_name],
        )

    def _compute_task_state(self, task_state):
        assert not task_state.is_blocked()
        task = task_state.task

        dep_keys = task.dep_keys
        dep_results = [
            self._task_states_by_key[dep_key]
                .results_by_name[dep_key.entity_name]
            for dep_key in dep_keys
        ]

        # All names should point to the same provider.
        provider, = set(
            self._flow_state.get_provider(task_key.entity_name)
            for task_key in task.keys
        )
        # And all the task keys should have the same case key.
        case_key, = set(task_key.case_key for task_key in task.keys)
        provenance = Provenance.from_computation(
            code_id=provider.get_code_id(case_key),
            case_key=case_key,
            dep_provenances_by_name={
                dep_result.query.name: dep_result.query.provenance
                for dep_result in dep_results
            },
        )
        # We'll use "tk" ("task key") to prefix lists that line up with our
        # list of task keys.
        tk_queries = [
            Query(
                name=task_key.entity_name,
                protocol=provider.protocol_for_name(task_key.entity_name),
                case_key=case_key,
                provenance=provenance,
            )
            for task_key in task.keys
        ]
        tk_loggable_task_strs = [
            self._loggable_str_for_task_key(task_key)
            for task_key in task.keys
        ]

        should_persist = provider.attrs.should_persist
        if should_persist:
            if not self._is_ready_for_full_resolution:
                raise AssertionError(
                    "Can't apply persistent caching to bootstrap entities %r"
                    % (tuple(provider.attrs.names),))
            tk_results = []
            for query, task_str in zip(tk_queries, tk_loggable_task_strs):
                result = self._persistent_cache.load(query)
                if result is not None:
                    self._log('Loaded    %s from file cache', task_str)
                    tk_results.append(result)
                else:
                    results_ready = False
                    break
            else:
                results_ready = True
        else:
            results_ready = False

        if not results_ready:
            for task_str in tk_loggable_task_strs:
                if not task.is_simple_lookup:
                    self._log('Computing %s ...', task_str)

            dep_values = [dep_result.value for dep_result in dep_results]

            tk_values = task_state.task.compute(dep_values)

            assert len(tk_values) == len(provider.attrs.names)

            tk_results = []
            for value, query, loggable_task_str in zip(
                    tk_values, tk_queries, tk_loggable_task_strs):
                query.protocol.validate(value)
                result = Result(query, value)
                if should_persist:
                    self._persistent_cache.save(result)
                    # We immediately reload the value and treat that as the
                    # real value.  That way, if the serialized/deserialized
                    # value is not exactly the same as the original, we still
                    # always return the same value.
                    result = self._persistent_cache.load(query)

                if task.is_simple_lookup:
                    self._log(
                        'Accessed  %s from definition', loggable_task_str)
                else:
                    self._log('Computed  %s', loggable_task_str)

                tk_results.append(result)

        assert len(tk_results) == len(task.keys)
        task_state.results_by_name = {
            task_key.entity_name: result
            for task_key, result in zip(task.keys, tk_results)
        }

    def _loggable_str_for_task_key(self, task_key):
        return '%s(%s)' % (
           task_key.entity_name,
           ', '.join(
               '%s=%s' % (name, value)
               for name, value in task_key.case_key.items())
        )

    def _log(self, message, *args):
        if self._is_ready_for_full_resolution:
            log_level = logging.INFO
        else:
            log_level = logging.DEBUG
        logger.log(log_level, message, *args)


class TaskState(object):
    def __init__(self, task):
        self.task = task
        self.results_by_name = None
        self.parents = []
        self.children = []

    def is_complete(self):
        return self.results_by_name is not None

    def is_blocked(self):
        return not all(parent.is_complete() for parent in self.parents)

    def __repr__(self):
        return 'TaskState(%r)' % self.task
