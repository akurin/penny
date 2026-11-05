"""Store registry and private branch configuration shared by transports and replies."""
import json
import os

from .logging_setup import event

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Branch addresses, store codes and verification tokens are deliberately NOT in
# the repository: they would reveal where the family lives. Local runs read
# config/stores.local.json (gitignored); a deployment mounts the same file from
# a Secret. Without it, branch verification fails closed.
STORES_CONFIG_PATH = os.environ.get("PENNY_STORES_CONFIG") or os.path.join(
    ROOT_DIR, "config", "stores.local.json")

SITES = {
    "cosmetic": {"base": "https://cosmetic.magnit.ru", "store": "",
                 "name": "М.Косметик", "address": "",
                 "adult": False, "verify": []},
    "magnit": {"base": "https://magnit.ru", "store": "",
               "name": "Магнит", "address": "",
               "adult": True, "verify": []},
    "dixy": {"base": "https://dixy.ru", "store": "",
             "name": "Дикси", "address": "",
             "adult": False, "verify": []},
    "5ka": {"base": "https://5ka.ru", "store": "",
            "name": "Пятёрочка", "address": "",
            "adult": False, "browser": True, "verify": []},
    "lenta": {"base": "https://lenta.com", "store": "", "alias": "",
              "name": "Лента", "address": "",
              "adult": False, "browser": True, "verify": []},
}


def load_store_config(path=None):
    """Merge branch-specific values from a local JSON file into SITES.

    Only known sites and keys are accepted; malformed files are ignored and
    leave the safe empty defaults in place."""
    try:
        with open(path or STORES_CONFIG_PATH, encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        event("stores_config_error", file=os.path.basename(str(path)))
        return False
    if not isinstance(data, dict):
        return False
    for site, values in data.items():
        if site not in SITES or not isinstance(values, dict):
            continue
        for key in ("store", "address", "alias"):
            value = values.get(key)
            if isinstance(value, (str, int)):
                SITES[site][key] = str(value)
        verify = values.get("verify")
        if isinstance(verify, list) and all(isinstance(item, str) for item in verify):
            SITES[site]["verify"] = verify
    return True


STORE_CONFIG_LOADED = load_store_config()

DEFAULT_SITES = ("cosmetic", "magnit", "dixy", "5ka", "lenta")
