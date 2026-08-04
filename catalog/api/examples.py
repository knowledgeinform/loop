"""Canonical payloads rendered in public documentation and exercised in tests."""

DOCUMENTED_EXPERIMENT_PAYLOAD = {
    "elements": {"Ho": 2, "Ti": 2, "O": 7},
    "structure_family": "pyrochlore",
    "phase_status": "single_phase",
    "comments": "Created through the documented LOOP API v1 example.",
    "synthesis_steps": [
        {
            "step_type": "ball_milling",
            "milling_time_hours": 8,
            "milling_rpm": 250,
            "ball_powder_ratio": "10:1",
            "atmosphere": "air",
        },
        {
            "step_type": "heat_treatment",
            "max_temp_c": 1500,
            "hold_time_hours": 6,
            "atmosphere": "air",
        },
    ],
}

DOCUMENTED_VALIDATION_PAYLOAD = {
    "record_type": "experiment",
    "record": DOCUMENTED_EXPERIMENT_PAYLOAD,
}

DOCUMENTED_LITERATURE_PAYLOAD = {
    "doi": "10.5555/loop.example.literature",
    "synthesis_successful": True,
    "elements": {"Er": 2, "Zr": 2, "O": 7},
    "structure_family": "pyrochlore",
    "title": "LOOP API literature example",
    "authors": ["Example Researcher"],
    "journal": "Example Journal",
    "year": 2026,
    "synthesis_steps": [
        {
            "step_type": "heat_treatment",
            "max_temp_c": 1450,
            "hold_time_hours": 8,
            "atmosphere": "air",
        }
    ],
}

DOCUMENTED_COMPUTATIONAL_PAYLOAD = {
    "elements": {"Dy": 2, "Ti": 2, "O": 7},
    "structure_family": "pyrochlore",
    "dft_source": "LOOP example",
    "calculation_method": "DFT",
    "functional": "PBE",
    "formation_energy_ev": -2.4,
    "bandgap_ev": 1.7,
    "spacegroup": "Fd-3m (#227)",
}

# A protocol stores a snapshot of the precursor details in its weighing step,
# not a reference to a mutable saved-library id. That makes the route portable
# and preserves exactly what was loaded into an experiment.
DOCUMENTED_PRECURSOR_PAYLOAD = {
    "name": "Titanium dioxide",
    "formula": "TiO2",
    "cas_number": "13463-67-7",
    "purity": "99.9%",
    "supplier": "Example supplier",
}

DOCUMENTED_PROTOCOL_PAYLOAD = {
    "name": "LOOP API oxide route",
    "description": "A reusable milling and heat-treatment route.",
    "steps": [
        {
            "step_type": "weighing",
            "total_mass_g": 2.0,
            "precursors_list": [DOCUMENTED_PRECURSOR_PAYLOAD],
        },
        {
            "step_type": "ball_milling",
            "milling_time_hours": 8,
            "milling_rpm": 250,
            "ball_powder_ratio": "10:1",
            "atmosphere": "air",
        },
        {
            "step_type": "heat_treatment",
            "max_temp_c": 1500,
            "hold_time_hours": 6,
            "atmosphere": "air",
        },
    ],
}
