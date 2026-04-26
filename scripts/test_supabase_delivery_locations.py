from __future__ import annotations

import json

from delivery_location import ensure_delivery_locations_table, get_delivery_location_status


def main() -> None:
    result = ensure_delivery_locations_table()
    status = get_delivery_location_status("default")
    print(json.dumps({"table": result, "status": status}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
