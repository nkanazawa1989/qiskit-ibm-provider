# This code is part of Qiskit.
#
# (C) Copyright IBM 2021.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Backend namespace for an IBM Quantum account."""

import copy
import logging
from datetime import datetime
from typing import Dict, List, Callable, Optional, Any, Union
from typing_extensions import Literal

from qiskit.providers.exceptions import QiskitBackendNotFoundError
from qiskit.providers.jobstatus import JobStatus
from qiskit.providers.providerutils import filter_backends

from qiskit_ibm_provider import ibm_provider  # pylint: disable=unused-import
from .api.exceptions import ApiError
from .apiconstants import ApiJobStatus
from .backendreservation import BackendReservation
from .exceptions import (
    IBMBackendValueError,
    IBMBackendApiError,
    IBMBackendApiProtocolError,
)
from .hub_group_project import HubGroupProject
from .ibm_backend import IBMBackend, IBMRetiredBackend
from .job import IBMJob, IBMCircuitJob
from .job.exceptions import IBMJobNotFoundError
from .utils.backend import convert_reservation_data
from .utils.converters import local_to_utc
from .utils.utils import to_python_identifier, validate_job_tags, filter_data
from .utils.hgp import to_instance_format

logger = logging.getLogger(__name__)


class IBMBackendService:
    """Backend namespace for an IBM Quantum account.

    Represent a namespace that provides backend related services for the IBM
    Quantum backends available to this account. An instance of
    this class is used as a callable attribute to the :class:`IBMProvider`
    class. This allows a convenient way to query for all backends or to access
    a specific backend::

        backends = provider.backends()  # Invoke backends() to get the backends.
        sim_backend = provider.backend.ibmq_qasm_simulator  # Get a specific backend instance.

    Also, you are able to retrieve jobs from an account without specifying the backend name.
    For example, to retrieve the ten most recent jobs you have submitted, regardless of the
    backend they were submitted to, you could do::

        most_recent_jobs = provider.backend.jobs(limit=10)

    It is also possible to retrieve a single job without specifying the backend name::

        job = provider.backend.retrieve_job(<JOB_ID>)
    """

    def __init__(
        self, provider: "ibm_provider.IBMProvider", hgp: HubGroupProject
    ) -> None:
        """IBMBackendService constructor.

        Args:
            provider: IBM Quantum account provider.
            hgp: default hub/group/project to use for the service.
        """
        super().__init__()
        self._provider = provider
        self._default_hgp = hgp
        self._backends: Dict[str, IBMBackend] = {}
        self._initialize_backends()
        self._discover_backends()

    def _initialize_backends(self) -> None:
        """Initialize the internal list of backends."""
        # Add backends from user selected hgp followed by backends
        # from other hgps if not already added
        for hgp in self._provider._get_hgps():
            for name, backend in hgp.backends.items():
                if name not in self._backends:
                    self._backends[name] = backend

    def _discover_backends(self) -> None:
        """Discovers the remote backends for this account, if not already known."""
        for backend in self._backends.values():
            backend_name = to_python_identifier(backend.name)
            # Append _ if duplicate
            while backend_name in self.__dict__:
                backend_name += "_"
            setattr(self, backend_name, backend)

    def backends(
        self,
        name: Optional[str] = None,
        filters: Optional[Callable[[List[IBMBackend]], bool]] = None,
        min_num_qubits: Optional[int] = None,
        input_allowed: Optional[Union[str, List[str]]] = None,
        instance: Optional[str] = None,
        **kwargs: Any,
    ) -> List[IBMBackend]:
        """Return all backends accessible via this account, subject to optional filtering.

        Args:
            name: Backend name to filter by.
            filters: More complex filters, such as lambda functions.
                For example::

                    IBMProvider.backends(
                        filters=lambda b: b.configuration().quantum_volume > 16)
            min_num_qubits: Minimum number of qubits the backend has to have.
            input_allowed: Filter by the types of input the backend supports.
                Valid input types are ``job`` (circuit job) and ``runtime`` (Qiskit Runtime).
                For example, ``inputs_allowed='runtime'`` will return all backends
                that support Qiskit Runtime. If a list is given, the backend must
                support all types specified in the list.
            instance: The provider in the hub/group/project format.
            **kwargs: Simple filters that specify a ``True``/``False`` criteria in the
                backend configuration, backends status, or provider credentials.
                An example to get the operational backends with 5 qubits::

                    IBMProvider.backends(n_qubits=5, operational=True)

        Returns:
            The list of available backends that match the filter.

        Raises:
            IBMBackendValueError: If only one or two parameters from `hub`, `group`,
                `project` are specified.
        """
        backends: List[IBMBackend] = []
        if instance:
            hgp = self._provider._get_hgp(instance=instance)
            backends = list(hgp.backends.values())
        else:
            backends = list(self._backends.values())
        # Special handling of the `name` parameter, to support alias resolution.
        if name:
            aliases = self._aliased_backend_names()
            aliases.update(self._deprecated_backend_names())
            name = aliases.get(name, name)
            kwargs["backend_name"] = name
        if min_num_qubits:
            backends = list(
                filter(lambda b: b.configuration().n_qubits >= min_num_qubits, backends)
            )
        if input_allowed:
            if not isinstance(input_allowed, list):
                input_allowed = [input_allowed]
            backends = list(
                filter(
                    lambda b: set(input_allowed)
                    <= set(b.configuration().input_allowed),
                    backends,
                )
            )
        return filter_backends(backends, filters=filters, **kwargs)

    def jobs(
        self,
        limit: Optional[int] = 10,
        skip: int = 0,
        backend_name: Optional[str] = None,
        status: Optional[
            Union[Literal["pending", "completed"], List[Union[JobStatus, str]]]
        ] = None,
        start_datetime: Optional[datetime] = None,
        end_datetime: Optional[datetime] = None,
        job_tags: Optional[List[str]] = None,
        descending: bool = True,
        instance: Optional[str] = None,
    ) -> List[IBMJob]:
        """Return a list of jobs, subject to optional filtering.

        Retrieve jobs that match the given filters and paginate the results
        if desired. Note that the server has a limit for the number of jobs
        returned in a single call. As a result, this function might involve
        making several calls to the server.

        Args:
            limit: Number of jobs to retrieve. ``None`` means no limit. Note that the
                number of sub-jobs within a composite job count towards the limit.
            skip: Starting index for the job retrieval.
            backend_name: Name of the backend to retrieve jobs from.
            status: Filter jobs with either "pending" or "completed" status.
            start_datetime: Filter by the given start date, in local time. This is used to
                find jobs whose creation dates are after (greater than or equal to) this
                local date/time.
            end_datetime: Filter by the given end date, in local time. This is used to
                find jobs whose creation dates are before (less than or equal to) this
                local date/time.
            job_tags: Filter by tags assigned to jobs. Matched jobs are associated with all tags.
            descending: If ``True``, return the jobs in descending order of the job
                creation date (i.e. newest first) until the limit is reached.
            instance: The provider in the hub/group/project format.

        Returns:
            A list of ``IBMJob`` instances.

        Raises:
            IBMBackendValueError: If a keyword value is not recognized.
            TypeError: If the input `start_datetime` or `end_datetime` parameter value
                is not valid.
        """
        # Build the filter for the query.

        api_filter = {}  # type: Dict[str, Any]
        if isinstance(status, list):
            if status in (["INITIALIZING"], ["VALIDATING"]):
                return []
            elif all(x in ["DONE", "CANCELLED", "ERROR"] for x in status):
                api_filter["pending"] = False
            elif all(x in ["QUEUED", "RUNNING"] for x in status):
                api_filter["pending"] = True
        if backend_name:
            api_filter["backend"] = backend_name
        if status == "pending":
            api_filter["pending"] = True
        if status == "completed":
            api_filter["pending"] = False
        if start_datetime:
            api_filter["created_after"] = local_to_utc(start_datetime).isoformat()
        if end_datetime:
            api_filter["created_before"] = local_to_utc(end_datetime).isoformat()
        if job_tags:
            validate_job_tags(job_tags, IBMBackendValueError)
            api_filter["job_tags"] = job_tags
        if instance:
            api_filter["provider"] = instance
        # Retrieve all requested jobs.
        job_responses = self._get_jobs(
            api_filter=api_filter, limit=limit, skip=skip, descending=descending
        )
        job_list = []
        for job_info in job_responses:
            # TODO filter by status
            job = self._restore_circuit_job(job_info, raise_error=False)
            if job is None:
                logger.warning(
                    'Discarding job "%s" because it contains invalid data.',
                    job_info.get("job_id", ""),
                )
                continue
            job_list.append(job)
        return job_list

    def _get_jobs(
        self,
        api_filter: Dict,
        limit: Optional[int] = 10,
        skip: int = 0,
        descending: bool = True,
    ) -> List:
        """Retrieve the requested number of jobs from the server using pagination.

        Args:
            api_filter: Filter used for querying.
            limit: Number of jobs to retrieve. ``None`` means no limit.
            skip: Starting index for the job retrieval.
            descending: If ``True``, return the jobs in descending order of the job
                creation date (i.e. newest first) until the limit is reached.

        Returns:
            A list of raw API response.
        """
        # Retrieve the requested number of jobs, using pagination. The server
        # might limit the number of jobs per request.
        job_responses: List[Dict[str, Any]] = []
        current_page_limit = limit if (limit is not None and limit <= 50) else 50
        while True:
            job_page = self._provider._runtime_client.jobs_get(
                limit=current_page_limit, skip=skip, descending=descending, **api_filter
            )["jobs"]
            if logger.getEffectiveLevel() is logging.DEBUG:
                filtered_data = [filter_data(job) for job in job_page]
                logger.debug("jobs() response data is %s", filtered_data)
            if not job_page:
                # Stop if there are no more jobs returned by the server.
                break
            job_responses += job_page
            if limit:
                if len(job_responses) >= limit:
                    # Stop if we have reached the limit.
                    break
                current_page_limit = limit - len(job_responses)
            else:
                current_page_limit = 50
            skip = len(job_responses)
        return job_responses

    def _restore_circuit_job(
        self, job_info: Dict, raise_error: bool
    ) -> Optional[IBMCircuitJob]:
        """Restore a circuit job from the API response.

        Args:
            job_info: Job info in dictionary format.
            raise_error: Whether to raise an exception if `job_info` is in
                an invalid format.

        Returns:
            Circuit job restored from the data, or ``None`` if format is invalid.

        Raises:
            IBMBackendApiProtocolError: If unexpected return value received
                 from the server.
        """
        job_params = {
            "job_id": job_info["id"],
            "creation_date": job_info["created"],
            "status": job_info["status"],
            "runtime_client": self._provider._runtime_client,
            "tags": job_info.get("tags"),
        }
        # Recreate the backend used for this job.
        backend_name = job_info.get("backend")
        instance = to_instance_format(
            job_info["hub"], job_info["group"], job_info["project"]
        )
        try:
            backend = self._provider.get_backend(backend_name, instance)
        except QiskitBackendNotFoundError:
            backend = IBMRetiredBackend.from_name(
                backend_name=backend_name,
                provider=self._provider,
                api=self._default_hgp._api_client,
            )
        try:
            job = IBMCircuitJob(
                backend=backend, api_client=self._default_hgp._api_client, **job_params
            )
            return job
        except TypeError as ex:
            if raise_error:
                raise IBMBackendApiProtocolError(
                    f"Unexpected return value received from the server "
                    f"when retrieving job {job_info['id']}: {ex}"
                ) from ex
        return None

    def job_ids(
        self,
        limit: Optional[int] = 10,
        skip: int = 0,
        backend_name: Optional[str] = None,
        status: Optional[Union[JobStatus, str, List[Union[JobStatus, str]]]] = None,
        start_datetime: Optional[datetime] = None,
        end_datetime: Optional[datetime] = None,
        job_tags: Optional[List[str]] = None,
        job_tags_operator: Optional[str] = "OR",
        descending: bool = True,
    ) -> List[IBMJob]:
        """Return a list of job IDs, subject to optional filtering.
        Retrieve jobs that match the given filters and paginate the results
        if desired. Note that the server has a limit for the number of jobs
        returned in a single call. As a result, this function might involve
        making several calls to the server.

        Args:
            limit: Number of jobs to retrieve. ``None`` means no limit.
            skip: Starting index for the job retrieval.
            backend_name: Name of the backend to retrieve jobs from.
            status: Only get jobs with this status or one of the statuses. For example, you can specify
                `status=JobStatus.RUNNING` or `status="RUNNING"` or `status=["RUNNING", "ERROR"]`
            start_datetime: Filter by the given start date, in local time. This is used to
                find jobs whose creation dates are after (greater than or equal to) this
                local date/time.
            end_datetime: Filter by the given end date, in local time. This is used to
                find jobs whose creation dates are before (less than or equal to) this
                local date/time.
            job_tags: Filter by tags assigned to jobs.
            job_tags_operator: Logical operator to use when filtering by job tags. Valid
                values are "AND" and "OR":

                    * If "AND" is specified, then a job must have all of the tags
                        specified in ``job_tags`` to be included.
                    * If "OR" is specified, then a job only needs to have any
                        of the tags specified in ``job_tags`` to be included.

            descending: If ``True``, return the jobs in descending order of the job
                creation date (i.e. newest first) until the limit is reached.

        Returns:
            A list of ``IBMJob`` instances.

        Raises:
            IBMBackendValueError: If a keyword value is not recognized.
            TypeError: If the input `start_datetime` or `end_datetime` parameter value is not valid.
        """
        # Build the filter for the query.
        api_filter = {}  # type: Dict[str, Any]

        if backend_name:
            api_filter["backend.name"] = backend_name

        if status:
            status_filter = self._get_status_db_filter(status)
            api_filter.update(status_filter)

        if start_datetime or end_datetime:
            api_filter["creationDate"] = self._update_creation_date_filter(
                cur_dt_filter={},
                gte_dt=local_to_utc(start_datetime).isoformat()
                if start_datetime
                else None,
                lte_dt=local_to_utc(end_datetime).isoformat() if end_datetime else None,
            )

        if job_tags:
            validate_job_tags(job_tags, IBMBackendValueError)
            job_tags_operator = job_tags_operator.upper()
            if job_tags_operator == "OR":
                api_filter["tags"] = {"inq": job_tags}
            elif job_tags_operator == "AND":
                and_tags = []
                for tag in job_tags:
                    and_tags.append({"tags": tag})
                api_filter["and"] = and_tags
            else:
                raise IBMBackendValueError(
                    '"{}" is not a valid job_tags_operator value. '
                    'Valid values are "AND" and "OR"'.format(job_tags_operator)
                )

        # Retrieve all requested jobs.
        jobs_id_list = self._get_job_ids(
            api_filter=api_filter, limit=limit, skip=skip, descending=descending
        )

        return jobs_id_list

    def _get_job_ids(
        self,
        api_filter: Dict,
        limit: Optional[int] = 10,
        skip: int = 0,
        descending: bool = True,
    ) -> List:
        """Retrieve the requested number of jobs IDs from the server using pagination.
        Args:
            api_filter: Filter used for querying.
            limit: Number of jobs to retrieve. ``None`` means no limit.
            skip: Starting index for the job retrieval.
            descending: If ``True``, return the jobs in descending order of the job
                creation date (i.e. newest first) until the limit is reached.
        Returns:
            A list of raw API response.
        """
        # Retrieve the requested number of jobs, using pagination.
        job_responses: List[Dict[str, Any]] = []
        current_page_limit = limit or 20
        initial_filter = copy.deepcopy(api_filter)

        while True:
            job_page = self._default_hgp._api_client.list_jobs_ids(
                limit=current_page_limit,
                skip=skip,
                descending=descending,
                extra_filter=api_filter,
            )
            if logger.getEffectiveLevel() is logging.DEBUG:
                filtered_data = list(job_page)
                logger.debug("jobs_id() response data is %s", filtered_data)

            if not job_page:
                # Stop if there are no more jobs returned by the server.
                break

            job_responses += job_page

            if limit:
                if len(job_responses) >= limit:
                    # Stop if we have reached the limit.
                    break
                current_page_limit = limit - len(job_responses)
            else:
                current_page_limit = 20

            # Use the last received job for pagination.
            skip = 0
            last_job = job_page[-1]
            api_filter = copy.deepcopy(initial_filter)
            cur_dt_filter = api_filter.pop("creationDate", {})
            if descending:
                new_dt_filter = self._update_creation_date_filter(
                    cur_dt_filter=cur_dt_filter, lte_dt=last_job["creationDate"]
                )
            else:
                new_dt_filter = self._update_creation_date_filter(
                    cur_dt_filter=cur_dt_filter, gte_dt=last_job["creationDate"]
                )
            if not cur_dt_filter:
                api_filter["creationDate"] = new_dt_filter
            else:
                self._merge_logical_filters(
                    api_filter,
                    {"and": [{"creationDate": new_dt_filter}, cur_dt_filter]},
                )

            if "id" not in api_filter:
                api_filter["id"] = {"nin": [last_job["id"]]}
            else:
                new_id_filter = {
                    "and": [
                        {"id": {"nin": [last_job["id"]]}},
                        {"id": api_filter.pop("id")},
                    ]
                }
                self._merge_logical_filters(api_filter, new_id_filter)

        return job_responses

    def _merge_logical_filters(self, cur_filter: Dict, new_filter: Dict) -> None:
        """Merge the logical operators in the input filters.

        Args:
            cur_filter: Current filter.
            new_filter: New filter to be merged into ``cur_filter``.

        Returns:
            ``cur_filter`` with ``new_filter``'s logical operators merged into it.
        """
        logical_operators_to_expand = ["or", "and"]
        for key in logical_operators_to_expand:
            if key in new_filter:
                if key in cur_filter:
                    cur_filter[key].extend(new_filter[key])
                else:
                    cur_filter[key] = new_filter[key]

    def _update_creation_date_filter(
        self,
        cur_dt_filter: Dict[str, Any],
        gte_dt: Optional[str] = None,
        lte_dt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Use the new start and end datetime in the creation date filter.

        Args:
            cur_dt_filter: Current creation date filter.
            gte_dt: New start datetime.
            lte_dt: New end datetime.

        Returns:
            Updated creation date filter.
        """
        if not gte_dt:
            gt_list = [
                cur_dt_filter.pop(gt_op)
                for gt_op in ["gt", "gte"]
                if gt_op in cur_dt_filter
            ]
            if "between" in cur_dt_filter and len(cur_dt_filter["between"]) > 0:
                gt_list.append(cur_dt_filter.pop("between")[0])
            gte_dt = max(gt_list) if gt_list else None
        if not lte_dt:
            lt_list = [
                cur_dt_filter.pop(lt_op)
                for lt_op in ["lt", "lte"]
                if lt_op in cur_dt_filter
            ]
            if "between" in cur_dt_filter and len(cur_dt_filter["between"]) > 1:
                lt_list.append(cur_dt_filter.pop("between")[1])
            lte_dt = min(lt_list) if lt_list else None
        new_dt_filter = {}  # type: Dict[str, Union[str, List[str]]]
        if gte_dt and lte_dt:
            new_dt_filter["between"] = [gte_dt, lte_dt]
        elif gte_dt:
            new_dt_filter["gte"] = gte_dt
        elif lte_dt:
            new_dt_filter["lte"] = lte_dt
        return new_dt_filter

    def _get_status_db_filter(
        self, status_arg: Union[JobStatus, str, List[Union[JobStatus, str]]]
    ) -> Dict[str, Any]:
        """Return the db filter to use when retrieving jobs based on a status or statuses.

        Returns:
            The status db filter used to query the api when retrieving jobs that match
            a given status or list of statuses.

        Raises:
            IBMBackendError: If a status value is not recognized.
        """
        _final_status_filter = None
        if isinstance(status_arg, list):
            _final_status_filter = {"or": []}
            for status in status_arg:
                status_filter = self._get_status_filter(status)
                _final_status_filter["or"].append(status_filter)
        else:
            status_filter = self._get_status_filter(status_arg)
            _final_status_filter = status_filter
        return _final_status_filter

    def _get_status_filter(self, status: Union[JobStatus, str]) -> Dict[str, Any]:
        """Return the db filter to use when retrieving jobs based on a status.

        Returns:
            The status db filter used to query the api when retrieving jobs
            that match a given status.

        Raises:
            IBMBackendValueError: If the status value is not recognized.
        """
        if isinstance(status, str):
            try:
                status = JobStatus[status.upper()]
            except KeyError:
                raise IBMBackendValueError(
                    '"{}" is not a valid status value. Valid values are {}'.format(
                        status, ", ".join(job_status.name for job_status in JobStatus)
                    )
                ) from None
        _status_filter = {}  # type: Dict[str, Any]
        if status == JobStatus.INITIALIZING:
            _status_filter = {
                "status": {
                    "inq": [ApiJobStatus.CREATING.value, ApiJobStatus.CREATED.value]
                }
            }
        elif status == JobStatus.VALIDATING:
            _status_filter = {
                "status": {
                    "inq": [ApiJobStatus.VALIDATING.value, ApiJobStatus.VALIDATED.value]
                }
            }
        elif status == JobStatus.RUNNING:
            _status_filter = {"status": ApiJobStatus.RUNNING.value}
        elif status == JobStatus.QUEUED:
            _status_filter = {"status": ApiJobStatus.QUEUED.value}
        elif status == JobStatus.CANCELLED:
            _status_filter = {"status": ApiJobStatus.CANCELLED.value}
        elif status == JobStatus.DONE:
            _status_filter = {"status": ApiJobStatus.COMPLETED.value}
        elif status == JobStatus.ERROR:
            _status_filter = {"status": {"regexp": "^ERROR"}}
        else:
            raise IBMBackendValueError(
                '"{}" is not a valid status value. Valid values are {}'.format(
                    status, ", ".join(job_status.name for job_status in JobStatus)
                )
            )
        return _status_filter

    def retrieve_job(self, job_id: str) -> IBMJob:
        """Return a single job.

        Args:
            job_id: The ID of the job to retrieve.

        Returns:
            The job with the given id.

        Raises:
            IBMBackendApiError: If an unexpected error occurred when retrieving
                the job.
            IBMBackendApiProtocolError: If unexpected return value received
                 from the server.
            IBMJobNotFoundError: If job cannot be found.
        """
        try:
            job_info = self._provider._runtime_client.job_get(job_id)
        except ApiError as ex:
            if "Error code: 3250." in str(ex):
                raise IBMJobNotFoundError(f"Job {job_id} not found.")
            raise IBMBackendApiError(
                "Failed to get job {}: {}".format(job_id, str(ex))
            ) from ex
        job = self._restore_circuit_job(job_info, raise_error=True)
        return job

    def my_reservations(self) -> List[BackendReservation]:
        """Return your upcoming reservations.

        Returns:
            A list of your upcoming reservations.
        """
        raw_response = self._default_hgp._api_client.my_reservations()
        return convert_reservation_data(raw_response)

    @staticmethod
    def _deprecated_backend_names() -> Dict[str, str]:
        """Returns deprecated backend names."""
        return {
            "ibmqx_qasm_simulator": "ibmq_qasm_simulator",
            "ibmqx_hpc_qasm_simulator": "ibmq_qasm_simulator",
            "real": "ibmqx1",
        }

    @staticmethod
    def _aliased_backend_names() -> Dict[str, str]:
        """Returns aliased backend names."""
        return {
            "ibmq_5_yorktown": "ibmqx2",
            "ibmq_5_tenerife": "ibmqx4",
            "ibmq_16_rueschlikon": "ibmqx5",
            "ibmq_20_austin": "QS1_1",
        }
