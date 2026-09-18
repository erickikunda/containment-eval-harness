import json
from pathlib import Path

import pytest

from containment.models import Scenario


@pytest.fixture
def scenario_data():
    path = Path(__file__).parents[1] / "examples" / "simulation.json"
    return json.loads(path.read_text())


@pytest.fixture
def scenario(scenario_data):
    return Scenario.model_validate_json(json.dumps(scenario_data))
