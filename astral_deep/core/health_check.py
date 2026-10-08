import asyncio
import httpx
import logging
from typing import Dict, List, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class ServiceStatus:
    name: str
    is_up: bool
    details: str
    actionable_error: str = ""

class SystemHealthChecker:
    """
    Verifica a prontidão dos serviços essenciais do AstralDeep.
    Evita o uso de segredos de produção, focando em conectividade e prontidão de API.
    """
    def __init__(self, base_url: str, services_config: Dict[str, str]):
        self.base_url = base_url.rstrip('/')
        self.services_config = services_config

    async def check_service(self, name: str, endpoint: str) -> ServiceStatus:
        url = f"{self.base_url}{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
                if response.status_code < 400:
                    return ServiceStatus(name, True, f"Service reachable (Status: {response.status_code})")
                else:
                    return ServiceStatus(
                        name, 
                        False, 
                        f"Service returned error {response.status_code}",
                        f"Check if {name} is running and configured correctly."
                    )
        except httpx.ConnectError:
            return ServiceStatus(
                name, 
                False, 
                "Connection refused", 
                f"Ensure {name} container/service is running and reachable at {url}"
            )
        except Exception as e:
            return ServiceStatus(name, False, str(e), "Check network connectivity and service logs.")

    async def run_all_checks(self) -> List[ServiceStatus]:
        tasks = [self.check_service(name, ep) for name, ep in self.services_config.items()]
        return await asyncio.gather(*tasks)
