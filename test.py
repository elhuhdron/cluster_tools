import cluster_tools
import subprocess
import concurrent.futures
import time
import sys
import logging
from enum import Enum
from functools import partial
import os
import pytest

# "Worker" functions.
def square(n):
    return n * n


def sleep(duration):
    time.sleep(duration)
    return duration

logging.basicConfig()

def raise_if(msg, bool):
    if bool:
        raise Exception("raise_if was called with True: {}".format(msg))


def get_executors():
    return [
        cluster_tools.get_executor(
            "slurm", debug=True, keep_logs=True, job_resources={"mem": "100M"}
        ),
        cluster_tools.get_executor("multiprocessing", max_workers=5),
        cluster_tools.get_executor("sequential"),
        cluster_tools.get_executor("test_pickling"),
        # cluster_tools.get_executor("pbs"),
    ]

@pytest.mark.skip(reason="The test is flaky on the CI for some reason. Disable it for now.")
def test_uncaught_warning():
    """
    This test ensures that there are warnings for "uncaught" futures.
    """

    # Log to a specific file which we can check for
    log_file_name = 'warning.log'
    logger = logging.getLogger('')
    logger.setLevel(logging.DEBUG)
    # create file handler which logs even debug messages
    if os.path.exists(log_file_name):
        os.remove(log_file_name)
    fh = logging.FileHandler(log_file_name)
    logger.addHandler(fh)

    cases = [False, True]

    def expect_marker(marker, msg, should_exist=True):
        maybe_negate = lambda b: b if should_exist else not b

        fh.flush()
        with open(log_file_name) as file:
            content = file.read()
            assert maybe_negate(marker in content), msg

    # In the following 4 cases we check whether there is a/no warning when using
    # map/submit with/without checking the futures.
    for exc in get_executors():
        marker = "map-expect-warning"
        with exc:
            exc.map(partial(raise_if, marker), cases)
        expect_marker(marker, "There should be a warning for an uncaught Future in map")

    for exc in get_executors():
        marker = "map-dont-expect-warning"
        with exc:
            try:
                list(exc.map(partial(raise_if, marker), cases))
            except Exception:
                pass
        expect_marker(marker, "There should be no warning for an uncaught Future in map", False)

    for exc in get_executors():
        marker = "submit-expect-warning"
        with exc:
            futures = [exc.submit(partial(raise_if, marker), b) for b in cases]
        expect_marker(marker, "There should be no warning for an uncaught Future in submit")

    for exc in get_executors():
        marker = "submit-dont-expect-warning"
        with exc:
            futures = [exc.submit(partial(raise_if, marker), b) for b in cases]
            try:
                for f in futures:
                    f.result()
            except Exception:
                pass
        expect_marker(marker, "There should be a warning for an uncaught Future in submit", False)

    logger.removeHandler(fh)


def test_submit():
    def run_square_numbers(executor):
        with executor:
            job_count = 3
            job_range = range(job_count)
            futures = [executor.submit(square, n) for n in job_range]
            for future, job_index in zip(futures, job_range):
                assert future.result() == square(job_index)

    for exc in get_executors():
        run_square_numbers(exc)


def test_unordered_sleep():
    """Get host identifying information about the servers running
    our jobs.
    """
    for exc in get_executors():
        with exc:
            durations = [10, 5]
            futures = [exc.submit(sleep, n) for n in durations]
            if not isinstance(exc, cluster_tools.SequentialExecutor):
                durations.sort()
            for duration, future in zip(
                durations, concurrent.futures.as_completed(futures)
            ):
                assert future.result() == duration


def test_unordered_map():
    for exc in get_executors():
        with exc:
            durations = [15, 1]
            results_gen = exc.map_unordered(sleep, durations)
            results = list(results_gen)

            if not isinstance(exc, cluster_tools.SequentialExecutor):
                durations.sort()

            for duration, result in zip(durations, results):
                assert result == duration

def test_map_to_futures():
    for exc in get_executors():
        with exc:
            durations = [15, 1]
            futures = exc.map_to_futures(sleep, durations)
            results = []

            for i, duration in enumerate(concurrent.futures.as_completed(futures)):
                results.append(duration.result())

            if not isinstance(exc, cluster_tools.SequentialExecutor):
                durations.sort()

            for duration, result in zip(durations, results):
                assert result == duration

def test_map():
    def run_map(executor):
        with executor:
            result = list(executor.map(square, [2, 3, 4]))
            assert result == [4, 9, 16]

    for exc in get_executors():
        run_map(exc)


def test_map_lazy():
    def run_map(executor):
        with executor:
            result = executor.map(square, [2, 3, 4])
        assert list(result) == [4, 9, 16]

    for exc in get_executors():
        run_map(exc)


def test_slurm_submit_returns_job_ids():
    exc = cluster_tools.get_executor("slurm", debug=True, keep_logs=True)
    with exc:
        future = exc.submit(square, 2)
        assert isinstance(future.cluster_jobid, int)
        assert future.cluster_jobid > 0
        assert future.result() == 4


def test_executor_args():
    def pass_with(exc):
        with exc:
            pass

    pass_with(cluster_tools.get_executor(
        "slurm", job_resources={"mem": "10M"}, non_existent_arg=True
    ))
    pass_with(cluster_tools.get_executor("multiprocessing", non_existent_arg=True))
    pass_with(cluster_tools.get_executor("sequential", non_existent_arg=True))

    # Test should succeed if the above lines don't raise an exception

test_output_str = "Test-Output"
def log(string):
    logging.debug(string)

def test_pickled_logging():

    def execute_with_log_level(log_level):
        logging_config = {
            "level": log_level,
        }
        with cluster_tools.get_executor(
            "slurm", debug=True, keep_logs=True, job_resources={"mem": "10M"}, logging_config=logging_config
        ) as executor:
            fut = executor.submit(log, test_output_str)
            fut.result()

            output = ".cfut/slurmpy.stdout.{}.log".format(fut.cluster_jobid)

            with open(output, 'r') as file:
                return file.read()

    debug_out = execute_with_log_level(logging.DEBUG)
    assert test_output_str in debug_out

    debug_out = execute_with_log_level(logging.INFO)
    assert not (test_output_str in debug_out)


class DummyEnum(Enum):
    BANANA = 0
    APPLE = 1
    PEAR = 2

def enum_consumer(value):
    assert value == DummyEnum.BANANA

def test_cloudpickle_serialization():
    enum_consumer_inner = enum_consumer

    for fn in [enum_consumer, enum_consumer_inner]:
        try:
            with cluster_tools.get_executor(
                "test_pickling"
            ) as executor:
                fut = executor.submit(fn, DummyEnum.BANANA)
            assert fn == enum_consumer
        except Exception:
            assert fn != enum_consumer

    assert True
