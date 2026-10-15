"""Local deterministic rule matching. Hits are retrieval hints, not legal opinions."""

import json
import operator
import re
from copy import deepcopy
from pathlib import Path


ASSETS = Path(__file__).resolve().parents[1] / "assets"


def asset(name):
    return json.loads((ASSETS / name).read_text(encoding="utf-8"))


def flatten(value):
    results = []
    if isinstance(value, dict):
        if value.get("序号") is not None and value.get("决策事项"):
            results.append(value)
        for child in value.values():
            results.extend(flatten(child))
    elif isinstance(value, list):
        for child in value:
            results.extend(flatten(child))
    return results


def lookup_rules(value):
    rules = {str(item["序号"]): item for item in flatten(asset("rules.json"))}
    ids = value.get("item_ids", [])
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise ValueError("item_ids must be strings")
    if set(ids) - set(rules):
        raise ValueError("Unknown checklist IDs; no fuzzy rule substitution allowed")
    return {"rules": [rules[key] for key in dict.fromkeys(ids)], "scope": "Source company decision checklist; confirm applicability"}


def condition(actual, expected):
    if actual is None:
        return False
    if isinstance(expected, str):
        match = re.fullmatch(r"(>=|<=|==|=|>|<)\s*(-?\d+(?:\.\d+)?)", expected)
        if match:
            operations = {">=": operator.ge, "<=": operator.le, "==": operator.eq, "=": operator.eq, ">": operator.gt, "<": operator.lt}
            try:
                return operations[match[1]](float(actual), float(match[2]))
            except (ValueError, TypeError):
                return False
    return actual == expected


def core_triggers(graph):
    schema = asset("graph_schema.json")
    entities = {item["id"]: item for item in graph.get("entities", [])}
    hits = []
    for rule in schema["CORE_TRIGGER_RULES"]:
        pattern = rule["pattern"]
        for relation in graph.get("relations", []):
            if relation.get("type") != pattern.get("relation_type"):
                continue
            source = entities.get(relation.get("from"), {})
            if pattern.get("entity_type") and source.get("type") != pattern["entity_type"]:
                continue
            props = relation.get("properties", relation.get("attributes", {}))
            source_props = source.get("properties", source.get("attributes", {}))
            if not all(condition(source_props.get(key) if key in {"is_state_owned", "is_listed"} else props.get(key), expected)
                       for key, expected in pattern.get("property_conditions", {}).items()):
                continue
            hits.append({"family": "enterprise", "rule_id": rule["rule_id"], "relation": relation, **rule["output"]})
    return {"fired_rules": hits, "triggered_concepts": list(dict.fromkeys(concept for hit in hits for concept in hit["legal_concepts"]))}


def normalize(value):
    ontology = asset("ontology.json")
    extraction = deepcopy(value["extraction"])
    entities = extraction.get("entities", [])
    ids = [item["id"] for item in entities]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate entity IDs within chunk")
    prefix = str(value.get("chunk_id", "chunk-0"))
    mapping = {key: f"{prefix}:{key}" for key in ids}
    for entity in entities:
        entity["id"] = mapping[entity["id"]]
    actions = []
    alias_map = {str(alias).strip().lower(): item["id"] for item in ontology["relations"]
                 for alias in [item["id"], *item.get("aliases", [])]}
    for relation in extraction.get("relations", extraction.get("relationships", [])):
        if relation.get("from") not in mapping or relation.get("to") not in mapping:
            raise ValueError("Relation points to an unknown entity")
        relation["from"] = mapping[relation["from"]]
        relation["to"] = mapping[relation["to"]]
        raw = relation.get("type", "UNKNOWN")
        actions.append({**relation, "canonical_action": alias_map.get(raw.strip().lower(), raw), "original_action": raw,
                        "attributes": relation.get("attributes", relation.get("properties", {}))})
    typed, concepts = [], []
    for entity in entities:
        match = next((item for item in ontology["concepts"] if entity.get("type") in item.get("entity_types", [])), None)
        method = "entity_type"
        if match is None:
            method = "alias_match"
            match = next((item for item in ontology["concepts"] if any(alias.strip() and alias.lower() in entity.get("name", "").lower()
                          for alias in item.get("aliases", []))), None)
        typed.append({"entity_id": entity["id"], "name": entity.get("name", ""), "original_type": entity.get("type", "UNKNOWN"),
                      "aligned_concept": match["id"] if match else "Unknown", "typing_method": method if match else "none",
                      "attributes": entity.get("attributes", entity.get("properties", {}))})
        if match:
            concepts.append({"entity_id": entity["id"], "concept_id": match["id"], "concept_name": match.get("name", match["id"])})
    types = {item["entity_id"]: item["original_type"] for item in typed}
    hits = []
    policy = value.get("rule_level_policy", {})
    for rule in ontology["rule_triggers"]:
        level = rule.get("rule_level", "NATIONAL")
        if level != "NATIONAL" and not (policy.get("allow_non_national") is True and level in policy.get("allowed_levels", [])):
            continue
        pattern = rule["pattern"]
        for action in actions:
            if pattern.get("action") and pattern["action"] != action["canonical_action"]:
                continue
            if pattern.get("from_entity_type") and pattern["from_entity_type"] != types.get(action["from"]):
                continue
            if pattern.get("to_entity_type") and pattern["to_entity_type"] != types.get(action["to"]):
                continue
            if all(condition(action["attributes"].get(key), expected) for key, expected in pattern.get("relation_conditions", {}).items()):
                hits.append({"family": "post_ner", "rule_id": rule["id"], "rule_level": level, "source_law": rule.get("source_law"),
                             "article": rule.get("article"), "matched_action": action, **rule["output"]})
    keywords = list(dict.fromkeys(concept for hit in hits for concept in hit["legal_concepts"]))
    keywords.extend(item["concept_name"] for item in concepts if item["concept_name"] not in keywords)
    law_names = list(dict.fromkeys(hit["source_law"] for hit in hits if hit["source_law"]))
    law_names.extend(item["name"] for item in typed if item["original_type"].upper() in {"LAW", "REGULATION", "LEGAL_DOCUMENT", "RULE"} and item["name"] not in law_names)
    articles = list(dict.fromkeys(str(item["attributes"][key]) for item in typed for key in ("article", "clause", "条", "款") if item["attributes"].get(key)))
    levels = list(dict.fromkeys(hit["rule_level"] for hit in hits))
    concepts_for_filters = list(dict.fromkeys([*keywords, *(item["concept_id"] for item in concepts)]))
    expected = {"law_name": law_names, "regulation_name": law_names, "article": articles, "clause": articles,
                "rule_level": levels, "concepts": concepts_for_filters, "tags": concepts_for_filters, "source_type": value.get("matched_categories", [])}
    strict, fallback = [], []
    for field, values in expected.items():
        strict.extend({field: item} for item in values[:8])
        if field in {"rule_level", "concepts", "source_type"}:
            fallback.extend({field: item} for item in values[:8])
    keywords = list(dict.fromkeys([*(item["name"] for item in typed if item["name"]), *(item["canonical_action"] for item in actions), *keywords, *law_names, *articles]))[:20]
    return {"chunk_id": prefix, "extraction": extraction, "typed_entities": typed, "normalized_actions": actions,
            "canonical_relations": [{**item, "canonical_type": item["canonical_action"]} for item in actions],
            "concept_mappings": concepts, "post_ner_trigger_results": {"fired_rules": hits, "triggered_concepts": list(dict.fromkeys(concept for hit in hits for concept in hit["legal_concepts"]))},
            "retrieval_filters": {"keywords": keywords, "strict_filters": strict, "fallback_filters": fallback, "expected": expected, "validation_mode": "soft"}}


def aggregate(value):
    chunks = value["chunks"]
    chunk_ids = [chunk["chunk_id"] for chunk in chunks]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("Duplicate chunk IDs")
    output = {"chunks": chunks, "legal_facts": [], "laws_involved": [], "uncertainty_notes": [], "evidence": []}
    for key in output:
        if key == "chunks":
            continue
        seen = {}
        for chunk in chunks:
            for item in chunk.get(key, []):
                identity = item.get("id") if key == "evidence" and isinstance(item, dict) else json.dumps(item, sort_keys=True, ensure_ascii=False)
                if identity in seen and seen[identity] != item:
                    raise ValueError("Conflicting evidence during aggregation")
                seen[identity] = item
        output[key] = list(seen.values())
    return output