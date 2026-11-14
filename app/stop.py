import json
import urllib.request

from .config import Settings


def main():
    settings = Settings()
    url = f"http://127.0.0.1:{settings['port']}"
    try:
        with urllib.request.urlopen(url + "/api/bootstrap", timeout=5) as response:
            bootstrap = json.load(response)
        request = urllib.request.Request(url + "/api/shutdown", data=b"{}", headers={"X-Workspace-Token": bootstrap["token"], "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            print(response.read().decode("utf-8"))
    except OSError:
        print("Local AI Workspace is not running.")


if __name__ == "__main__":
    main()
