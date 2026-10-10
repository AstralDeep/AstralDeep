"""A2A-compliant FHIR agent provides patient measurements, population queries and clinical dashboards over an operator-configured server.
Tools are dispatched through mcp_server.py and registered in-process by
orchestrator/local_agents.py only when the fhir feature flag is on.
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.base_agent import BaseA2AAgent
from shared.feature_flags import flags

from agents.fhir.mcp_server import MCPServer

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

DISABLED_EXIT_CODE = 78


class FhirAgent(BaseA2AAgent):
    agent_id = "fhir-1"
    service_name = "FHIR Clinical Data"
    description = (
        "Separate patient-level FHIR REST measurements and synthetic population-level SQL on FHIR queries. "
        "Also reads an HL7 FHIR R5 clinical feed: ICU census, patient overviews, "
        "vital sign trends, laboratory results, medications and timelines, plus "
        "live vital sign and activity streams. Read-only."
    )
    examples = [
        {"title": "Patient A1C",
         "prompt": "What is the latest A1C for MyHealthSafe demo patient DEMO-1001?"},
        {"title": "Population A1C",
         "prompt": "What is the average latest A1C among pregnant women in Wolfe County and Casey County, "
                   "Kentucky, in the synthetic reference dataset?"},
        {"title": "ICU census",
         "prompt": "Show the current ICU census from the FHIR feed"},
        {"title": "Patient overview",
         "prompt": "Open the FHIR patient overview for the ICU patient with the most concerning vital signs"},
        {"title": "Vital sign trends",
         "prompt": "Chart the last 12 hours of vital signs for the ICU patient with the most "
                   "concerning vital signs in the FHIR feed"},
        {"title": "Live activity",
         "prompt": "Watch the live ICU activity feed for five minutes"},
    ]
    skill_tags = ["fhir", "hl7", "clinical", "icu", "sql-on-fhir"]

    def __init__(self, port: int = None):
        super().__init__(MCPServer(), port=port, port_env_var="FHIR_AGENT_PORT")


def main() -> None:
    import argparse

    if not flags.is_enabled("fhir"):
        raise SystemExit(DISABLED_EXIT_CODE)
    parser = argparse.ArgumentParser(description='FHIR Clinical Data agent')
    parser.add_argument('--port', type=int, default=None, help='Port to run the agent on')
    args = parser.parse_args()
    asyncio.run(FhirAgent(port=args.port).run())


if __name__ == "__main__":
    main()
