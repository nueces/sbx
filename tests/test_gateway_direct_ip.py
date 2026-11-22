import json
from pathlib import Path

import pytest

from sbx import gateway
from sbx.runtime import ConfigError

CASES = json.loads(
    (Path(__file__).parents[1] / "gateway/testdata/direct-ip-cases.json").read_text()
)


@pytest.mark.parametrize(
    "case", CASES, ids=lambda case: f"{case['mode']}-{case['host']}-{case.get('allow_ranges', [])}"
)
def test_direct_ip_policy_contract(case):
    policy = {"id": "target", "mode": case["mode"], "hosts": [case["host"]], "ports": [443]}
    if "allow_ranges" in case:
        policy["allow_ranges"] = case["allow_ranges"]
    document = {"network": {"policy": [policy]}}
    if case["allowed"]:
        assert gateway.parse_config(document) is not None
    else:
        with pytest.raises(ConfigError):
            gateway.parse_config(document)


@pytest.mark.parametrize(
    "hosts",
    [["93.184.216.34"], ["93.184.216.34", "api.example.com"], ["api.example.com", "93.184.216.34"]],
)
def test_ip_binding_rejected_for_every_expanded_host(hosts):
    with pytest.raises(ConfigError, match="DNS"):
        gateway.parse_config(
            {
                "network": {
                    "policy": [
                        {"id": "target", "mode": "http-mediated", "hosts": hosts, "ports": [443]}
                    ],
                    "credential": [
                        {
                            "id": "token",
                            "environment": "TOKEN",
                            "method": "command",
                            "fetch": "startup",
                            "options": {"argv": ["must-not-run"]},
                            "bindings": [
                                {
                                    "policies": ["target"],
                                    "header": "Authorization: Bearer {placeholder}",
                                }
                            ],
                        }
                    ],
                }
            }
        )
