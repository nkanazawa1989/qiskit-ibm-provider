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

"""Custom JSON decoder."""

from typing import Dict, Union, List, Any
import json

import dateutil.parser
from qiskit.providers.models import (
    PulseDefaults,
    BackendProperties,
)

from .converters import utc_to_local, utc_to_local_all


def defaults_from_server_data(defaults: Dict) -> PulseDefaults:
    """Decode pulse defaults data.

    Args:
        defaults: Raw pulse defaults data.

    Returns:
        A ``PulseDefaults`` instance.
    """
    for item in defaults["pulse_library"]:
        _decode_pulse_library_item(item)

    for cmd in defaults["cmd_def"]:
        if "sequence" in cmd:
            for instr in cmd["sequence"]:
                _decode_pulse_qobj_instr(instr)

    return PulseDefaults.from_dict(defaults)


def properties_from_server_data(properties: Dict) -> BackendProperties:
    """Decode backend properties.

    Args:
        properties: Raw properties data.

    Returns:
        A ``BackendProperties`` instance.
    """
    properties["last_update_date"] = dateutil.parser.isoparse(
        properties["last_update_date"]
    )
    for qubit in properties["qubits"]:
        for nduv in qubit:
            nduv["date"] = dateutil.parser.isoparse(nduv["date"])
    for gate in properties["gates"]:
        for param in gate["parameters"]:
            param["date"] = dateutil.parser.isoparse(param["date"])
    for gen in properties["general"]:
        gen["date"] = dateutil.parser.isoparse(gen["date"])

    properties = utc_to_local_all(properties)
    return BackendProperties.from_dict(properties)


def decode_pulse_qobj(pulse_qobj: Dict) -> None:
    """Decode a pulse Qobj.

    Args:
        pulse_qobj: Qobj to be decoded.
    """
    for item in pulse_qobj["config"]["pulse_library"]:
        _decode_pulse_library_item(item)

    for exp in pulse_qobj["experiments"]:
        for instr in exp["instructions"]:
            _decode_pulse_qobj_instr(instr)


def decode_backend_configuration(config: Dict) -> None:
    """Decode backend configuration.

    Args:
        config: A ``QasmBackendConfiguration`` or ``PulseBackendConfiguration``
            in dictionary format.
    """
    config["online_date"] = dateutil.parser.isoparse(config["online_date"])

    if "u_channel_lo" in config:
        for u_channle_list in config["u_channel_lo"]:
            for u_channle_lo in u_channle_list:
                u_channle_lo["scale"] = _to_complex(u_channle_lo["scale"])


def decode_result(result: str, result_decoder: Any) -> Dict:
    """Decode result data.

    Args:
        result: Run result in string format.
        result_decoder: A decoder class for loading the json
    """
    result_dict = json.loads(result, cls=result_decoder)
    if "date" in result:
        if isinstance(result_dict["date"], str):
            result_dict["date"] = dateutil.parser.isoparse(result_dict["date"])
        result_dict["date"] = utc_to_local(result_dict["date"])
    return result_dict


def _to_complex(value: Union[List[float], complex]) -> complex:
    """Convert the input value to type ``complex``.

    Args:
        value: Value to be converted.

    Returns:
        Input value in ``complex``.

    Raises:
        TypeError: If the input value is not in the expected format.
    """
    if isinstance(value, list) and len(value) == 2:
        return complex(value[0], value[1])
    elif isinstance(value, complex):
        return value

    raise TypeError("{} is not in a valid complex number format.".format(value))


def _decode_pulse_library_item(pulse_library_item: Dict) -> None:
    """Decode a pulse library item.

    Args:
        pulse_library_item: A ``PulseLibraryItem`` in dictionary format.
    """
    pulse_library_item["samples"] = [
        _to_complex(sample) for sample in pulse_library_item["samples"]
    ]


def _decode_pulse_qobj_instr(pulse_qobj_instr: Dict) -> None:
    """Decode a pulse Qobj instruction.

    Args:
        pulse_qobj_instr: A ``PulseQobjInstruction`` in dictionary format.
    """
    if "val" in pulse_qobj_instr:
        pulse_qobj_instr["val"] = _to_complex(pulse_qobj_instr["val"])
    if "parameters" in pulse_qobj_instr and "amp" in pulse_qobj_instr["parameters"]:
        pulse_qobj_instr["parameters"]["amp"] = _to_complex(
            pulse_qobj_instr["parameters"]["amp"]
        )
