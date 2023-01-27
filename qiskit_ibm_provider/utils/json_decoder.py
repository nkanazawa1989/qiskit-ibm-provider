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
import warnings
from typing import Dict, Tuple, Union, List, Any, Optional
import json

import dateutil.parser
from qiskit.providers.models import (
    QasmBackendConfiguration,
    PulseBackendConfiguration,
    PulseDefaults,
    BackendProperties,
    Command,
)
from qiskit.providers.models.backendproperties import Gate as GateSchema
from qiskit.circuit.controlflow import IfElseOp, WhileLoopOp, ForLoopOp
from qiskit.circuit.gate import Gate, Instruction
from qiskit.circuit.library.standard_gates import get_standard_gate_name_mapping
from qiskit.pulse.calibration_entries import PulseQobjDef
from qiskit.transpiler.target import Target, InstructionProperties
from qiskit.qobj.pulse_qobj import PulseLibraryItem
from qiskit.qobj.converters.pulse_instruction import QobjToInstructionConverter
from qiskit.utils import apply_prefix

from .converters import utc_to_local, utc_to_local_all
from ..ibm_qubit_properties import IBMQubitProperties


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


def target_from_server_data(
    configuration: Union[QasmBackendConfiguration, PulseBackendConfiguration],
    pulse_defaults: Optional[Dict] = None,
    properties: Optional[Dict] = None,
) -> Target:
    """Decode transpiler target from backend data set.

    This function directly generate ``Target`` instance without generate
    intermediate legacy objects such as ``BackendProperties`` and ``PulseDefaults``.

    Args:
        configuration: Backend configuration.
        pulse_defaults: Backend pulse defaults dictionary.
        properties: Backend property dictionary.

    Returns:
        A ``Target`` instance.
    """
    in_data = {"num_qubits": configuration.n_qubits}

    # Parse global configuration properties
    if hasattr(configuration, "dt"):
        in_data["dt"] = configuration.dt
    if hasattr(configuration, "timing_constraints"):
        consts = configuration.timing_constraints
        in_data["granularity"] = consts["granularity"]
        in_data["min_length"] = consts["min_length"]
        in_data["pulse_alignment"] = consts["pulse_alignment"]
        in_data["aquire_alignment"] = consts["acquire_alignment"]

    # Load Qiskit object representation
    qiskit_inst_mapping = get_standard_gate_name_mapping()
    qiskit_control_flow_mapping = {
        "if_else": IfElseOp,
        "while_loop": WhileLoopOp,
        "for_loop": ForLoopOp,
    }

    # Create instruction property placeholder from backend configuration
    supported_instructions = set(getattr(configuration, "supported_instructions", []))
    basis_gates = set(getattr(configuration, "basis_gates", []))
    gate_configs = {gate.name: gate for gate in configuration.gates}
    inst_name_map = {}  # type: Dict[str, Instruction]
    prop_name_map = {}  # type: Dict[str, Dict[Tuple[int, ...], InstructionProperties]]

    all_instructions = set.union(supported_instructions, basis_gates)
    for name in all_instructions:
        if name in qiskit_control_flow_mapping:
            continue
        if name in qiskit_inst_mapping:
            inst_name_map[name] = qiskit_inst_mapping[name]
        elif name in gate_configs:
            this_config = gate_configs[name]
            params = getattr(this_config, "parameters", [])
            coupling_map = getattr(this_config, "coupling_map", [])
            inst_name_map[name] = Gate(
                name=name,
                num_qubits=len(coupling_map[0]) if coupling_map else 0,
                params=params,
            )
        else:
            warnings.warn(
                f"Definition of instruction {name} is not found in Qiskit and "
                "no gate configuration is provided by the backend. "
                "This instruction is not created in Target.",
                UserWarning,
            )
            all_instructions.remove(name)
            continue
        if name in gate_configs:
            coupling_map = getattr(gate_configs[name], "coupling_map", [])
            prop_name_map[name] = dict.fromkeys(map(tuple, coupling_map))
        elif name in ("measure", "delay"):
            # Special cases
            # These are not gates but all qubits support these instructions in IBM
            prop_name_map[name] = {(q,): None for q in range(configuration.n_qubits)}
        else:
            # Broadcast to all qubits. This is a special syntax of Target.
            prop_name_map[name] = {None: None}

    # Populate gate properties
    if properties:
        in_data["qubit_properties"] = list(
            map(_decode_qubit_property, properties["qubits"])
        )
        for gate_spec in map(GateSchema.from_dict, properties["gates"]):
            inst_prop = _decode_instruction_property(gate_spec)
            qubits = tuple(gate_spec.qubits)
            if gate_spec.gate not in all_instructions:
                # New instruction is found in property. Likely an edge case.
                new_instruction = Gate(
                    gate_spec.gate,
                    num_qubits=len(qubits),
                    params=[],
                )
                prop_name_map[gate_spec.gate] = {}
                inst_name_map[gate_spec.gate] = new_instruction
                all_instructions.add(gate_spec.gate)
            prop_name_map[gate_spec.gate][qubits] = inst_prop
        # Measure instruction property is stored in qubit property
        measure_props = list(map(_decode_measure_property, properties["qubits"]))
        for qubit, measure_prop in enumerate(measure_props):
            qubits = (qubit,)
            prop_name_map["measure"][qubits] = measure_prop

    # Define pulse qobj converter and command sequence for lazy conversion
    if pulse_defaults:
        pulse_lib = list(
            map(PulseLibraryItem.from_dict, pulse_defaults["pulse_library"])
        )
        converter = QobjToInstructionConverter(pulse_lib)
        for cmd in map(Command.from_dict, pulse_defaults["cmd_def"]):
            name = cmd.name
            qubits = tuple(cmd.qubits)
            if name not in all_instructions or qubits not in prop_name_map[name]:
                # Ignore pulse gate.
                continue
            entry = PulseQobjDef(converter=converter, name=cmd.name)
            entry.define(cmd.sequence)
            prop_name_map[name][qubits].calibration = entry

    # Add parsed properties to target
    target = Target(**in_data)
    for inst_name in all_instructions:
        if inst_name in qiskit_control_flow_mapping:
            # Control flow operator doesn't have gate property.
            target.add_instruction(
                instruction=qiskit_control_flow_mapping[inst_name],
                name=inst_name,
            )
        else:
            target.add_instruction(
                instruction=inst_name_map[inst_name],
                properties=prop_name_map[inst_name],
            )

    return target


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
    if "date" in result_dict:
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


def _decode_qubit_property(qubit_specs: List[Dict]) -> IBMQubitProperties:
    """Decode qubit property data to generate IBMQubitProperty instance.

    Args:
        qubit_specs: List of qubit property dictionary.

    Returns:
        An ``IBMQubitProperty`` instance.
    """
    in_data = {}
    for spec in qubit_specs:
        name = spec["name"]
        if name in IBMQubitProperties.__slots__:
            in_data[name] = apply_prefix(
                value=spec["value"], unit=spec.get("unit", None)
            )
    return IBMQubitProperties(**in_data)  # type: ignore[no-untyped-call]


def _decode_instruction_property(gate_spec: GateSchema) -> InstructionProperties:
    """Decode gate property data to generate InstructionProperties instance.

    Args:
        gate_spec: List of gate property dictionary.

    Returns:
        An ``InstructionProperties`` instance.
    """
    in_data = {}
    for param in gate_spec.parameters:
        if param.name == "gate_error":
            in_data["error"] = param.value
        if param.name == "gate_length":
            in_data["duration"] = apply_prefix(value=param.value, unit=param.unit)
    return InstructionProperties(**in_data)


def _decode_measure_property(qubit_specs: List[Dict]) -> InstructionProperties:
    """Decode qubit property data to generate InstructionProperties instance.

    Args:
        qubit_specs: List of qubit property dictionary.

    Returns:
        An ``InstructionProperties`` instance.
    """
    in_data = {}
    for spec in qubit_specs:
        name = spec["name"]
        if name == "readout_error":
            in_data["error"] = spec["value"]
        if name == "readout_length":
            in_data["duration"] = apply_prefix(
                value=spec["value"], unit=spec.get("unit", None)
            )
    return InstructionProperties(**in_data)
