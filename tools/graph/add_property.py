"""Create or update a Property node and wire all relationships.

Properties are physical addresses with ownership/listing history.
They connect people to places and timelines -- purchase and sale dates
are as meaningful as employment dates.

Phase 7 of tool-upgrade-plan-v3.md.
---
description: Create Property with ownership and listing history
creates_nodes: [Property]
creates_edges: [OWNED_BY, LISTED_BY, SUPPORTED_BY]
databases: [corcoran]
---

Backward-compatible wrapper around node_ops + wire_evidence.
Preserves the original parameter signature while delegating to generic operations.
"""
import sys
import re
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, execute_write, GRAPH_DATABASE
from lib.io import setup_output, load_params, output
from tools.graph.node_ops import node_impl
from tools.graph.wire_evidence import wire_evidence_impl


VALID_PROPERTY_TYPES = {
    "residential", "commercial", "land", "mixed-use", "other",
}

# Normalize common street suffix abbreviations
SUFFIX_MAP = {
    "street": "St", "st": "St", "st.": "St",
    "avenue": "Ave", "ave": "Ave", "ave.": "Ave",
    "drive": "Dr", "dr": "Dr", "dr.": "Dr",
    "road": "Rd", "rd": "Rd", "rd.": "Rd",
    "lane": "Ln", "ln": "Ln", "ln.": "Ln",
    "boulevard": "Blvd", "blvd": "Blvd", "blvd.": "Blvd",
    "court": "Ct", "ct": "Ct", "ct.": "Ct",
    "circle": "Cir", "cir": "Cir", "cir.": "Cir",
    "place": "Pl", "pl": "Pl", "pl.": "Pl",
    "way": "Way",
    "terrace": "Ter", "ter": "Ter", "ter.": "Ter",
    "trail": "Trl", "trl": "Trl", "trl.": "Trl",
}


def _normalize_address(address):
    """Light address normalization: strip unit/apt, normalize suffixes."""
    if not address:
        return address

    # Strip leading/trailing whitespace
    addr = address.strip()

    # Remove apartment/unit/suite suffixes for base property matching
    # Keep the original for display but MERGE on base
    addr = re.sub(r',?\s*(apt|unit|suite|ste|#)\s*\S+$', '', addr, flags=re.IGNORECASE)

    # Normalize street suffixes
    parts = addr.split()
    normalized = []
    for part in parts:
        lower = part.lower().rstrip('.')
        if lower in SUFFIX_MAP:
            normalized.append(SUFFIX_MAP[lower])
        else:
            normalized.append(part)

    return ' '.join(normalized)


def add_property_impl(address, city="", state="", zip_code="",
                      property_type="residential", description="",
                      source="", ownership=None, listings=None,
                      parcel_id="", current_value="",
                      extra_props=None, sources=None,
                      database=GRAPH_DATABASE, driver=None, **kwargs):
    """Create or update a Property node and wire all relationships.

    Args:
        address: Street address (normalized for MERGE)
        city: City name
        state: State abbreviation
        zip_code: ZIP code
        property_type: One of VALID_PROPERTY_TYPES
        description: What this property is / why it matters
        source: Lifestream entry ID that sourced this
        ownership: List of {owner, start, end, price, salePrice, current} dicts
        listings: List of {agent, brokerage, date, price, status} dicts
        parcel_id: County parcel/APN number
        current_value: Current assessed/market value string
        extra_props: Additional properties dict
        sources: List of {url, confidence, claim} for SUPPORTED_BY edges
        database: Neo4j database (default: corcoran)
        driver: Optional shared Neo4j driver

    Returns:
        dict with created, updated, edges_wired, warnings
    """
    ownership = ownership or []
    listings = listings or []
    extra_props = extra_props or {}
    sources = sources or []

    if property_type not in VALID_PROPERTY_TYPES:
        property_type = "other"

    # Normalize address for consistent MERGE
    norm_address = _normalize_address(address)
    city_norm = city.strip().title() if city else ""
    state_norm = state.strip().upper() if state else ""
    zip_norm = zip_code.strip() if zip_code else ""

    # Build full address for display
    full_parts = [norm_address]
    if city_norm:
        full_parts.append(city_norm)
    if state_norm:
        full_parts.append(state_norm)
    if zip_norm:
        full_parts.append(zip_norm)
    full_address = ", ".join(full_parts)

    _driver = driver or get_neo4j_driver()
    result = {"created": False, "updated": False, "edges_wired": 0, "warnings": []}

    try:
        # 1. MERGE Property node via node_impl
        # Property merge key is [address, city, state]
        node_props = {
            "address": norm_address,
            "city": city_norm,
            "state": state_norm,
            "fullAddress": full_address,
            "propertyType": property_type,
        }
        if zip_norm:
            node_props["zip"] = zip_norm
        if description:
            node_props["description"] = description
        if source:
            node_props["source"] = source
        if parcel_id:
            node_props["parcelId"] = parcel_id
        if current_value:
            node_props["currentValue"] = current_value
        node_props.update({k: v for k, v in extra_props.items() if v is not None})

        node_result = node_impl("add", "Property", database=database, driver=_driver,
                                **node_props)
        if "error" in node_result:
            return node_result

        result["created"] = node_result.get("created", False)
        result["updated"] = node_result.get("updated", False)
        result["full_address"] = full_address
        result["warnings"].extend(node_result.get("warnings", []))

        # 2. Wire OWNED_BY edges (temporal ownership history)
        for o in ownership:
            owner_name = o.get("owner", "")
            if not owner_name:
                continue

            start = o.get("start", "")
            end = o.get("end", "")
            price = o.get("price", "")
            sale_price = o.get("salePrice", o.get("sale_price", ""))
            current = o.get("current", False)

            records, _ = execute_write(
                "MATCH (p:Property {address: $addr, city: $city, state: $state}) "
                "MATCH (n {name: $owner}) "
                "MERGE (p)-[r:OWNED_BY]->(n) "
                "SET r.startDate = CASE WHEN $start <> '' THEN $start ELSE r.startDate END, "
                "    r.endDate = CASE WHEN $end <> '' THEN $end ELSE r.endDate END, "
                "    r.purchasePrice = CASE WHEN $price <> '' THEN $price ELSE r.purchasePrice END, "
                "    r.salePrice = CASE WHEN $sale <> '' THEN $sale ELSE r.salePrice END, "
                "    r.current = $current, "
                "    r.source = $source "
                "RETURN n.name AS matched",
                database=database, driver=_driver,
                addr=norm_address, city=city_norm, state=state_norm,
                owner=owner_name, start=start, end=end,
                price=price, sale=sale_price, current=current,
                source=source,
            )
            if records:
                result["edges_wired"] += 1
            else:
                result["warnings"].append(
                    f"Owner '{owner_name}' not found for OWNED_BY edge. "
                    "Create with add_person/add_organization first."
                )

        # 3. Wire LISTED_BY edges (agent listings)
        for l in listings:
            agent_name = l.get("agent", "")
            if not agent_name:
                continue

            brokerage = l.get("brokerage", "")
            date = l.get("date", "")
            price = l.get("price", "")
            status = l.get("status", "")

            records, _ = execute_write(
                "MATCH (p:Property {address: $addr, city: $city, state: $state}) "
                "MATCH (n {name: $agent}) "
                "MERGE (p)-[r:LISTED_BY]->(n) "
                "SET r.brokerage = CASE WHEN $brokerage <> '' THEN $brokerage ELSE r.brokerage END, "
                "    r.date = CASE WHEN $date <> '' THEN $date ELSE r.date END, "
                "    r.listPrice = CASE WHEN $price <> '' THEN $price ELSE r.listPrice END, "
                "    r.status = CASE WHEN $status <> '' THEN $status ELSE r.status END, "
                "    r.source = $source "
                "RETURN n.name AS matched",
                database=database, driver=_driver,
                addr=norm_address, city=city_norm, state=state_norm,
                agent=agent_name, brokerage=brokerage,
                date=date, price=price, status=status,
                source=source,
            )
            if records:
                result["edges_wired"] += 1
            else:
                result["warnings"].append(
                    f"Agent '{agent_name}' not found for LISTED_BY edge. "
                    "Create with add_person first."
                )

        # 4. Wire SUPPORTED_BY edges via wire_evidence
        if sources:
            ev_result = wire_evidence_impl(
                entity=norm_address, sources=sources, label="Property",
                # Property has composite key -- need custom match clause
                match_clause="MATCH (n:Property {address: $name, city: $city, state: $state})",
                extra_params={"city": city_norm, "state": state_norm},
                database=database, driver=_driver
            )
            if "error" not in ev_result:
                result["edges_wired"] += ev_result.get("edges_wired", 0)
                result["supported_by_wired"] = ev_result.get("edges_wired", 0)
                result["warnings"].extend(ev_result.get("warnings", []))
            else:
                result["warnings"].append(f"Evidence wiring failed: {ev_result['error']}")

    except Exception as e:
        result["error"] = str(e)
        import traceback
        result["traceback"] = traceback.format_exc()
    finally:
        if not driver:
            _driver.close()

    return result


if __name__ == "__main__":
    setup_output()
    p = load_params()
    r = add_property_impl(
        address=p["address"], city=p.get("city", ""),
        state=p.get("state", ""), zip_code=p.get("zip_code", ""),
        property_type=p.get("property_type", "residential"),
        description=p.get("description", ""), source=p.get("source", ""),
        ownership=p.get("ownership", []), listings=p.get("listings", []),
        parcel_id=p.get("parcel_id", ""), current_value=p.get("current_value", ""),
        extra_props=p.get("extra_props", {}), sources=p.get("sources", []),
        database=p.get("database", GRAPH_DATABASE),
    )
    output(r)
