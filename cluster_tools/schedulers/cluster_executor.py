from concurrent import futures
import os
from cluster_tools.util import (
    random_string,
    FileWaitThread,
    enrich_future_with_uncaught_warning,
    get_function_name,
    with_preliminary_postfix,
)
import threading
import signal
import sys
from cluster_tools import pickling
from cluster_tools.pickling import file_path_to_absolute_module
import time
from abc import abstractmethod
import logging
from typing import Union
from cluster_tools.tailf import Tail
import shutil


#https://stackoverflow.com/questions/312443/how-do-you-split-a-list-into-evenly-sized-chunks
from itertools import zip_longest
def grouper(n, iterable, padvalue=None):
    "grouper(3, 'abcdefg', 'x') --> ('a','b','c'), ('d','e','f'), ('g','x','x')"
    return zip_longest(*[iter(iterable)]*n, fillvalue=padvalue)

class RemoteException(Exception):
    def __init__(self, error, job_id):
        self.error = error
        self.job_id = job_id

    def __str__(self):
        return str(self.job_id) + "\n" + self.error.strip()


class ClusterExecutor(futures.Executor):
    """Futures executor for executing jobs on a cluster."""

    def __init__(
        self,
        debug=False,
        keep_logs=True,
        cfut_dir=None,
        job_resources=None,
        job_name=None,
        additional_setup_lines=[],
        **kwargs,
    ):
        """
        `kwargs` can be the following optional parameters:
            `logging_config`: An object containing a `level` key specifying the desired log level and/or a
                `format` key specifying the desired log format string. Cannot be specified together
                with `logging_setup_fn`.
            `logging_setup_fn`: A function setting up custom logging. The function will be called for
                remotely executed code (slurm, pbs) to re-setup logging. The function will be called with the
                default log file name. If the caller sets up file logging, this log file name should be adapted,
                for example, by adding a .mylog suffix. Cannot be specified together with `logging_config`.
        """
        self.debug = debug
        self.additional_setup_lines = additional_setup_lines
        self.job_name = job_name
        self.was_requested_to_shutdown = False
        self.cfut_dir = (
            cfut_dir if cfut_dir is not None else os.getenv("CFUT_DIR", ".cfut")
        )
        self.files_to_clean_up = []

        logging.info(
            f"Instantiating ClusterExecutor. Log files are stored in {self.cfut_dir}"
        )

        # handle maximum job size limit (e.g. MaxArraySize in slurm).
        if 'maxjobsize' in job_resources:
            self.maxjobsize = int(job_resources['maxjobsize'])
            del job_resources['maxjobsize']
        else:
            self.maxjobsize = None
        # handle maximum submitted jobs limit (e.g. MaxSubmitJobs in slurm).
        if 'maxsubmit' in job_resources:
            self.maxsubmit = int(job_resources['maxsubmit'])
            del job_resources['maxsubmit']
        else:
            self.maxsubmit = None
        self.job_resources = job_resources

        # `jobs` maps from job id to (future, workerid, outfile_name, should_keep_output)
        # In case, job arrays are used: job id and workerid are in the format of
        # `job_id-job_index` and `workerid-job_index`.
        self.jobs = {}
        self.job_outfiles = {}
        self.jobs_lock = threading.Lock()
        self.jobs_empty_cond = threading.Condition(self.jobs_lock)
        self.keep_logs = keep_logs

        self.wait_thread = FileWaitThread(self._completion, self)
        self.wait_thread.start()

        os.makedirs(self.cfut_dir, exist_ok=True)

        signal.signal(signal.SIGINT, self.handle_kill)
        signal.signal(signal.SIGTERM, self.handle_kill)

        self.meta_data = {}
        assert not (
            "logging_config" in kwargs and "logging_setup_fn" in kwargs
        ), "Specify either logging_config OR logging_setup_fn but not both at once"
        if "logging_config" in kwargs:
            self.meta_data["logging_config"] = kwargs["logging_config"]
        if "logging_setup_fn" in kwargs:
            self.meta_data["logging_setup_fn"] = kwargs["logging_setup_fn"]

    def handle_kill(self, signum, frame):
        self.wait_thread.stop()
        job_ids = ",".join(str(id) for id in self.jobs.keys())
        print(
            "A termination signal was registered. The following jobs are still running on the cluster:\n{}".format(
                job_ids
            )
        )
        sys.exit(130)

    @abstractmethod
    def check_for_crashed_job(self, job_id) -> Union["failed", "ignore", "completed"]:
        pass

    def _start(self, workerid, job_count=None, job_name=None):
        """Start a job with the given worker ID and return an ID
        identifying the new job. The job should run ``python -m
        cfut.remote <workerid>.
        """

        jobid = self.inner_submit(
            f"{sys.executable} -m cluster_tools.remote {workerid} {self.cfut_dir}",
            job_name=self.job_name if self.job_name is not None else job_name,
            additional_setup_lines=self.additional_setup_lines,
            job_count=job_count,
        )

        return jobid

    @abstractmethod
    def inner_submit(self, *args, **kwargs):
        pass

    def _cleanup(self, jobid):
        """Given a job ID as returned by _start, perform any necessary
        cleanup after the job has finished.
        """
        if self.keep_logs:
            return

        outf = self.format_log_file_path(self.cfut_dir, jobid)
        self.files_to_clean_up.append(outf)

    @staticmethod
    @abstractmethod
    def format_log_file_name(jobid, suffix=".stdout"):
        pass

    @classmethod
    def format_log_file_path(cls, cfut_dir, jobid, suffix=".stdout"):
        return os.path.join(cfut_dir, cls.format_log_file_name(jobid, suffix))

    @classmethod
    @abstractmethod
    def get_job_id_string(self):
        pass

    def get_temp_file_path(self, file_name):
        return os.path.join(self.cfut_dir, file_name)

    @staticmethod
    def format_infile_name(cfut_dir, job_id):
        return os.path.join(cfut_dir, "cfut.in.%s.pickle" % job_id)

    @staticmethod
    def format_outfile_name(cfut_dir, job_id):
        return os.path.join(cfut_dir, "cfut.out.%s.pickle" % job_id)

    def _completion(self, jobid, failed_early):
        """Called whenever a job finishes."""
        with self.jobs_lock:
            job_info = self.jobs.pop(jobid)
            if len(job_info) == 4:
                fut, workerid, outfile_name, should_keep_output = job_info
            else:
                # Backwards compatibility
                fut, workerid = job_info
                should_keep_output = False
                outfile_name = self.format_outfile_name(self.cfut_dir, workerid)

            if not self.jobs:
                self.jobs_empty_cond.notify_all()
        if self.debug:
            print("job completed: {}".format(jobid), file=sys.stderr)

        preliminary_outfile_name = with_preliminary_postfix(outfile_name)
        if failed_early:
            # If the code which should be executed on a node wasn't even
            # started (e.g., because python isn't installed or the cluster_tools
            # couldn't be found), no output was written to disk. We only noticed
            # this circumstance because the whole job was marked as failed.
            # Therefore, we don't try to deserialize pickling output.
            success = False
            result = "Job submission/execution failed. Please look into the log file at {}".format(
                self.format_log_file_path(self.cfut_dir, jobid)
            )
        else:
            with open(preliminary_outfile_name, "rb") as f:
                outdata = f.read()
            success, result = pickling.loads(outdata)

        if success:
            # Remove the .preliminary postfix since the job was finished
            # successfully. # Therefore, the result can be used as a checkpoint
            # by users of the clustertools.
            os.rename(preliminary_outfile_name, outfile_name)
            logging.debug("Pickle file renamed to {}.".format(outfile_name))

            fut.set_result(result)
        else:
            fut.set_exception(RemoteException(result, jobid))

        # Clean up communication files.

        infile_name = self.format_infile_name(self.cfut_dir, workerid)
        self.files_to_clean_up.append(infile_name)
        if not should_keep_output:
            self.files_to_clean_up.append(outfile_name)

        self._cleanup(jobid)

    def ensure_not_shutdown(self):
        if self.was_requested_to_shutdown:
            raise RuntimeError(
                "submit() was invoked on a ClusterExecutor instance even though shutdown() was executed for that instance."
            )

    def create_enriched_future(self):
        fut = futures.Future()
        enrich_future_with_uncaught_warning(fut)
        return fut

    def submit(self, fun, *args, **kwargs):
        """
        Submit a job to the pool.
        kwargs may contain __cfut_options which currently should look like:
        {
            "output_pickle_path": str
        }
        output_pickle_path defines where the pickled result should be stored.
        That file will not be removed after the job has finished.
        """
        fut = self.create_enriched_future()
        workerid = random_string()

        should_keep_output = False
        if "__cfut_options" in kwargs:
            should_keep_output = True
            output_pickle_path = kwargs["__cfut_options"]["output_pickle_path"]
            del kwargs["__cfut_options"]
        else:
            output_pickle_path = self.format_outfile_name(self.cfut_dir, workerid)

        self.ensure_not_shutdown()

        # Start the job.
        serialized_function_info = pickling.dumps(
            (fun, args, kwargs, self.meta_data, output_pickle_path)
        )
        with open(self.format_infile_name(self.cfut_dir, workerid), "wb") as f:
            f.write(serialized_function_info)

        self.store_main_path_to_meta_file(workerid)

        preliminary_output_pickle_path = with_preliminary_postfix(output_pickle_path)
        if os.path.exists(preliminary_output_pickle_path):
            logging.warning(
                f"Deleting stale output file at {preliminary_output_pickle_path}..."
            )
            os.unlink(preliminary_output_pickle_path)

        job_name = get_function_name(fun)
        jobid = self._start(workerid, job_name=job_name)

        if self.debug:
            print("job submitted: %i" % jobid, file=sys.stderr)

        # Thread will wait for it to finish.
        self.wait_thread.waitFor(preliminary_output_pickle_path, jobid)

        with self.jobs_lock:
            self.jobs[jobid] = (fut, workerid, output_pickle_path, should_keep_output)

        fut.cluster_jobid = jobid
        return fut

    def get_workerid_with_index(self, workerid, index):
        return workerid + "_" + str(index)

    def get_jobid_with_index(self, jobid, index):
        return str(jobid) + "_" + str(index)

    def get_function_pickle_path(self, workerid):
        return self.format_infile_name(
            self.cfut_dir, self.get_workerid_with_index(workerid, "function")
        )

    @staticmethod
    def get_main_meta_path(cfut_dir, workerid):
        return os.path.join(cfut_dir, f"cfut.main_path.{workerid}.txt")

    def store_main_path_to_meta_file(self, workerid):
        with open(self.get_main_meta_path(self.cfut_dir, workerid), "w") as file:
            file.write(file_path_to_absolute_module(sys.argv[0]))

    def map_to_futures(self, fun, allArgs, output_pickle_path_getter=None):
        self.ensure_not_shutdown()
        allArgs = list(allArgs)
        if len(allArgs) == 0:
            return []

        should_keep_output = output_pickle_path_getter is not None

        futs_with_output_paths = []

        # Group jobs into groups of maxjobsize
        if self.maxjobsize is not None:
            groups_allArgs = list(grouper(self.maxjobsize, allArgs))
            groups_allArgs = [[x for x in y if x is not None] for y in groups_allArgs]
        else:
            groups_allArgs = [allArgs]

        # Submit completely separate jobs for each group in order to comply with maxjobsize
        for job_index, group_allArgs in enumerate(groups_allArgs):
            workerid = random_string()

            pickled_function_path = self.get_function_pickle_path(workerid)
            self.files_to_clean_up.append(pickled_function_path)
            with open(pickled_function_path, "wb") as file:
                pickling.dump(fun, file)
            self.store_main_path_to_meta_file(workerid)

            # Submit jobs eagerly
            cfuts_with_output_paths = []
            for index, arg in enumerate(group_allArgs):
                fut = self.create_enriched_future()
                workerid_with_index = self.get_workerid_with_index(workerid, index)

                if output_pickle_path_getter is None:
                    output_pickle_path = self.format_outfile_name(
                        self.cfut_dir, workerid_with_index
                    )
                else:
                    output_pickle_path = output_pickle_path_getter(arg)

                preliminary_output_pickle_path = with_preliminary_postfix(
                    output_pickle_path
                )
                if os.path.exists(preliminary_output_pickle_path):
                    logging.warning(
                        f"Deleting stale output file at {preliminary_output_pickle_path}..."
                    )
                    os.unlink(preliminary_output_pickle_path)

                # Start the job.
                serialized_function_info = pickling.dumps(
                    (pickled_function_path, [arg], {}, self.meta_data, output_pickle_path)
                )
                infile_name = self.format_infile_name(self.cfut_dir, workerid_with_index)

                with open(infile_name, "wb") as f:
                    f.write(serialized_function_info)

                futs_with_output_paths.append((fut, output_pickle_path))
                cfuts_with_output_paths.append((fut, output_pickle_path))
            #for index, arg in enumerate(group_allArgs):

            job_count = len(group_allArgs)
            job_name = get_function_name(fun)

            if self.maxsubmit is not None:
                # wait for enough jobs to complete so that maxsubmit limit will not be exceeded.
                cfuts = [fut for (fut, _) in futs_with_output_paths]
                nsubmitted = len(cfuts)
                if nsubmitted > self.maxsubmit:
                    print('Waiting to submit {} jobs, maxsubmit={}'.format(job_count, self.maxsubmit),
                        file=sys.stderr)
                    for fut in futures.as_completed(cfuts):
                        nsubmitted -= 1
                        if nsubmitted <= self.maxsubmit: break

            jobid = self._start(workerid, job_count, job_name)

            if self.debug:
                print(
                    "job %i/%i submitted: %i. consists of %i subjobs." % (job_index+1, len(groups_allArgs), jobid,
                        job_count),
                    file=sys.stderr,
                )

            for index, (fut, output_pickle_path) in enumerate(cfuts_with_output_paths):
                jobid_with_index = self.get_jobid_with_index(jobid, index)
                # Thread will wait for it to finish.

                outfile_name = output_pickle_path
                self.wait_thread.waitFor(
                    with_preliminary_postfix(outfile_name), jobid_with_index
                )

                fut.cluster_jobid = jobid
                fut.cluster_jobindex = index

                # do not put lock around the whole for loop because this results in a race condition where both
                #   this thread and FileWaitThread attempt to grab both locks at the same time.
                # in this thread both locks would be attempted in waitFor.
                # in FileWaitThread both locks would be attempted in _complete.
                # these threads can now be running simultaneously when self.maxsubmit is specified.
                with self.jobs_lock:
                    self.jobs[jobid_with_index] = (
                        fut,
                        workerid_with_index,
                        outfile_name,
                        should_keep_output,
                    )
        #for job_index, group_allArgs in enumerate(groups_allArgs):

        return [fut for (fut, _) in futs_with_output_paths]

    def shutdown(self, wait=True):
        """Close the pool."""
        self.was_requested_to_shutdown = True
        if wait:
            with self.jobs_lock:
                if self.jobs:
                    self.jobs_empty_cond.wait()

        self.wait_thread.stop()
        self.wait_thread.join()

        for file_to_clean_up in self.files_to_clean_up:
            try:
                os.unlink(file_to_clean_up)
            except OSError:
                pass
        self.files_to_clean_up = []

    def map(self, func, args, timeout=None, chunksize=None):
        if chunksize is not None:
            logging.warning(
                "The provided chunksize parameter is ignored by ClusterExecutor."
            )

        start_time = time.time()

        futs = self.map_to_futures(func, args)

        # Return a separate generator as an iterator to avoid that the
        # map() method itself becomes a generator (due to the usage of yield).
        # If map() was a generator, the submit() calls would be invoked
        # lazily which can lead to a shutdown of the executor before
        # the submit calls are performed.
        def result_generator():
            for fut in futs:
                passed_time = time.time() - start_time
                remaining_timeout = None if timeout is None else timeout - passed_time
                yield fut.result(remaining_timeout)

        return result_generator()

    def map_unordered(self, func, args):
        futs = self.map_to_futures(func, args)

        # Return a separate generator to avoid that map_unordered
        # is executed lazily.
        def result_generator():
            for fut in futures.as_completed(futs):
                yield fut.result()

        return result_generator()

    def forward_log(self, fut):
        """
        Takes a future from which the log file is forwarded to the active
        process. This method blocks as long as the future is not done.
        """

        log_path = self.format_log_file_path(self.cfut_dir, fut.cluster_jobid)
        # Don't use a logger instance here, since the child process
        # probably already used a logger.
        log_callback = lambda s: sys.stdout.write(f"(jid={fut.cluster_jobid}) {s}")
        tailer = Tail(log_path, log_callback)
        fut.add_done_callback(lambda _: tailer.cancel())

        # Poll until the log file exists
        while not (os.path.exists(log_path) or tailer.is_cancelled):
            time.sleep(2)

        # Log the output of the log file until future is resolved
        # by the done_callback we attached earlier.
        tailer.follow(2)
        return fut.result()

    @abstractmethod
    def get_pending_tasks(self):
        pass
