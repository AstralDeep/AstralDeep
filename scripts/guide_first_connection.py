import asyncio
import sys
import argparse
from astral_deep.core.health_check import SystemHealthChecker

async def run_onboarding(endpoint: str):
    print(f"\n--- AstralDeep First Connection Guide ---")
    print(f"Target Endpoint: {endpoint}")
    print("Verifying system readiness...\n")

    # Configuração de endpoints baseada na arquitetura padrão
    services_to_check = {
        "Database (PostgreSQL)": "/health/db",
        "Authentication (Keycloak)": "/health/auth",
        "Task Worker": "/health/worker",
        "Core API": "/health/api"
    }

    checker = SystemHealthChecker(endpoint, services_to_check)
    results = await checker.run_all_checks()

    all_passed = True
    for res in results:
        status_icon = "✅" if res.is_up else "❌"
        print(f"{status_icon} {res.name}: {res.details}")
        if not res.is_up:
            print(f"   💡 Action: {res.actionable_error}")
            all_passed = False

    print("\n" + "="*40)
    if all_passed:
        print("🎉 SUCCESS: All core services are operational.")
        print("You can now proceed to sign-in via the web interface.")
        print("="*40 + "\n")
    else:
        print("⚠️ FAILURE: Some services are not ready.")
        print("Please check your docker-compose logs or service configurations.")
        print("="*40 + "\n")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Guide first client connection to AstralDeep.")
    parser.add_argument("--endpoint", required=True, help="The base URL of the AstralDeep deployment (e.g., http://localhost:8000)")
    
    args = parser.parse_args()
    
    try:
        asyncio.run(run_onboarding(args.endpoint))
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(1)

if __name__ == "__main__":
    main()
