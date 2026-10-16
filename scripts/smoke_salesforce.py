"""Step 1b acceptance: authenticate with client credentials and run one SOQL query."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arfollow.salesforce import SalesforceClient  # noqa: E402


def main() -> None:
    sf = SalesforceClient()
    sf.authenticate()
    print(f"Instance:    {sf.instance_url}")
    print(f"API version: v{sf.api_version}")
    info = sf.userinfo()
    print(f"Run-as user: {info.get('preferred_username')} ({info.get('name')})")
    count = sf.get("query", q="SELECT COUNT() FROM Account")["totalSize"]
    print(f"SOQL OK:     {count} Account records visible")
    sample = list(sf.query("SELECT Id, Name FROM Account ORDER BY CreatedDate DESC LIMIT 3"))
    for rec in sample:
        print(f"  {rec['Id']}  {rec['Name']}")
    try:
        sf.create_many("Account", [{"Name": "should not happen"}])
    except PermissionError as e:
        print(f"Read-only guard OK: {e}")


if __name__ == "__main__":
    main()
