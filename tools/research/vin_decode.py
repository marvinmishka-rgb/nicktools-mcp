"""
vin_decode -- Decode a VIN using NHTSA vPIC API with optional recalls and complaints.

Standalone vehicle identification tool. Decodes any 17-character VIN to extract
make, model, year, body class, engine specs, plant info, and more. Optionally
fetches safety recalls and owner complaints for the decoded vehicle.

For plate-to-VIN conversion, see Phase 6 (BeenVerified integration) or use
PlateToVIN API when configured.

Designed as an in-process tool (_impl function) for fast dispatch.
---
description: Decode VIN via NHTSA -- returns make, model, year, engine, recalls, complaints
databases: []
read_only: true
optional: true
domain_extension: Vehicle research -- standalone NHTSA VIN decoder with recalls and complaints. Example of wrapping a free public API as a tool. Safe to remove if not needed.
---
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Import shared HTTP and NHTSA functions from search_records
from tools.research.search_records import (
    _decode_nhtsa_vin, _get_nhtsa_recalls, _get_nhtsa_complaints, _is_vin
)


def vin_decode_impl(vin=None, model_year=None, include_recalls=False,
                    include_complaints=False, driver=None, **kwargs):
    """Decode a Vehicle Identification Number using NHTSA vPIC API.

    Args:
        vin (required): 17-character Vehicle Identification Number
        model_year: Optional model year for better decode accuracy on ambiguous VINs
        include_recalls: If true, also fetch safety recalls for this vehicle (default false)
        include_complaints: If true, also fetch owner complaints (default false)
        driver: Ignored (dispatch compatibility)

    Returns:
        dict with decoded vehicle data, plus optional recalls and complaints

    Example:
        research("vin_decode", {"vin": "2FMDK4KC18BA47928"})
        research("vin_decode", {"vin": "3C6UR5PL9RG402343", "include_recalls": true})
    """
    if not vin:
        return {"error": "Missing required parameter: vin"}

    vin = vin.strip().upper()

    if not _is_vin(vin):
        return {"error": f"Invalid VIN format: '{vin}'. Must be 17 alphanumeric characters (no I, O, Q)."}

    # Decode the VIN
    result = _decode_nhtsa_vin(vin, model_year=model_year)
    if result.get("error"):
        return result

    # Build a one-line vehicle summary
    parts = [result.get("year", ""), result.get("make", ""), result.get("model", "")]
    if result.get("trim"):
        parts.append(result["trim"])
    result["vehicle_summary"] = " ".join(p for p in parts if p)

    # Optional: fetch recalls
    if include_recalls and result.get("make") and result.get("model") and result.get("year"):
        recalls = _get_nhtsa_recalls(result["make"], result["model"], result["year"])
        result["recalls"] = recalls

    # Optional: fetch complaints
    if include_complaints and result.get("make") and result.get("model") and result.get("year"):
        complaints = _get_nhtsa_complaints(result["make"], result["model"], result["year"])
        result["complaints"] = complaints

    return result


def main():
    """Subprocess entry point."""
    if len(sys.argv) < 2:
        print("ERROR: Missing params file path", file=sys.stderr)
        sys.exit(1)

    try:
        with open(sys.argv[1], 'r', encoding='utf-8') as f:
            p = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load params file: {e}", file=sys.stderr)
        sys.exit(1)

    result = vin_decode_impl(**p)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
