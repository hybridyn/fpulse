"""
Rule-based Planner — converts natural language intent to IR.

No LLM required. Uses keyword matching + templates for common patterns.
Handles 80% of demo cases. Falls back gracefully for complex intents.
"""

from __future__ import annotations

import re
from typing import Any

from fpulse.ir.schema import Workflow, Step, StepConnection, StepType, NodePosition


class PlannerResult:
    def __init__(self, workflow: Workflow | None = None, confidence: float = 0.0,
                 explanation: str = "", needs_ai: bool = False):
        self.workflow = workflow
        self.confidence = confidence
        self.explanation = explanation
        self.needs_ai = needs_ai

    def dict(self):
        return {
            "workflow": self.workflow.model_dump(mode="json") if self.workflow else None,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "needs_ai": self.needs_ai,
        }


class RulePlanner:
    """Intent-to-IR planner using keyword matching and templates."""

    def plan(self, intent: str) -> PlannerResult:
        """Convert a natural language intent to a workflow IR."""
        intent_lower = intent.lower().strip()

        # Detect operations from intent
        ops = self._detect_operations(intent_lower)
        source = self._detect_source(intent_lower, intent)

        if not source:
            return PlannerResult(
                confidence=0.2,
                explanation="Could not determine data source. Please specify a file or table.",
                needs_ai=True,
            )

        # Build workflow from detected operations
        steps: list[Step] = []
        connections: list[StepConnection] = []
        x_pos = 0

        # Step 1: Source node
        source_step = self._create_source_step(source, intent, x_pos)
        steps.append(source_step)
        prev_id = source_step.id
        x_pos += 350

        # Step 2+: Operation nodes
        for op in ops:
            step = self._create_operation_step(op, intent, intent_lower, x_pos)
            if step:
                steps.append(step)
                connections.append(StepConnection(from_step=prev_id, to_step=step.id))
                prev_id = step.id
                x_pos += 350

        # Final: Output node
        output_step = self._create_output_step(intent_lower, x_pos)
        steps.append(output_step)
        connections.append(StepConnection(from_step=prev_id, to_step=output_step.id))

        # Generate a name from the intent
        name = self._generate_name(intent)

        workflow = Workflow(
            name=name,
            description=intent,
            steps=steps,
            connections=connections,
        )

        confidence = min(0.5 + len(ops) * 0.15, 0.95)

        explanation_parts = [f"Source: {source['type']}"]
        for op in ops:
            explanation_parts.append(f"→ {op['type'].title()}")
        explanation_parts.append(f"→ Output")

        return PlannerResult(
            workflow=workflow,
            confidence=confidence,
            explanation=" ".join(explanation_parts),
        )

    def _detect_operations(self, intent: str) -> list[dict[str, Any]]:
        """Detect operations from intent keywords."""
        ops = []

        # Filter
        filter_patterns = [
            r"(?:where|filter|only|exclude)\s+(.+?)(?:\s*,|\s+(?:and\s+then|then|and\s+\w+)|$)",
        ]
        for pat in filter_patterns:
            m = re.search(pat, intent)
            if m and not any(o["type"] == "filter" for o in ops):
                hint = m.group(1).strip()
                # Strip leading SQL keywords that user might include naturally
                hint = re.sub(r'^(?:where|having)\s+', '', hint, flags=re.IGNORECASE)
                ops.append({"type": "filter", "hint": hint})
                break

        # Deduplicate — capture column names, stop at operation keywords
        _stop_words = r"(?:,\s*(?:calculate|compute|filter|transform|output|write|save|aggregate|group|then|and\s+(?:then|calculate|output)))"
        dedup_patterns = [
            r"(?:dedup(?:licate)?|remove\s+duplicates?|distinct)\s*(?:on|by)\s+([\w]+(?:\s*,\s*(?!calculate|compute|filter|output|write|aggregate|group)[\w]+)*)",
            r"(?:dedup(?:licate)?|remove\s+duplicates?|distinct)\b",
        ]
        for pat in dedup_patterns:
            m = re.search(pat, intent)
            if m and not any(o["type"] == "deduplicate" for o in ops):
                key_hint = m.group(1).strip().rstrip(",").strip() if m.lastindex and m.group(1) else ""
                ops.append({"type": "deduplicate", "hint": key_hint})
                break

        # Aggregate — look for revenue/sum/count/daily keywords
        has_agg = any(w in intent for w in [
            "aggregate", "group by", "sum", "count", "average", "avg",
            "total", "revenue", "daily", "weekly", "monthly",
        ])
        # "calculate" only triggers aggregate if paired with agg-related words
        if not has_agg and "calculate" in intent:
            has_agg = any(w in intent for w in ["revenue", "total", "sum", "average", "daily", "count"])
        if has_agg and not any(o["type"] == "aggregate" for o in ops):
            hint_match = re.search(r"(?:daily|weekly|monthly)\s+([\w\s]+?)(?:\s*,|\s+(?:and|then|output)|$)", intent)
            hint = hint_match.group(1).strip() if hint_match else ""
            ops.append({"type": "aggregate", "hint": hint})

        # Transform — only if explicit transform keywords (not "calculate")
        transform_patterns = [
            r"(?:transform|convert|rename|add\s+column)\s+(.+?)(?:\s*,|\s+(?:and|then)|$)",
            r"(?:clean|normalize|lowercase|uppercase|trim)\s*(.*?)(?:\s*,|$)",
        ]
        for pat in transform_patterns:
            m = re.search(pat, intent)
            if m and not any(o["type"] == "transform" for o in ops):
                ops.append({"type": "transform", "hint": m.group(1).strip() if m.group(1) else ""})
                break

        # If no operations detected, check for simple patterns
        if not ops:
            if any(w in intent for w in ["load", "read", "ingest", "import"]):
                pass  # Just source → output
            else:
                ops.append({"type": "transform", "hint": "custom"})

        return ops

    def _detect_source(self, intent_lower: str, intent_original: str) -> dict[str, Any] | None:
        """Detect data source from intent."""
        # CSV file pattern
        csv_match = re.search(r'([\w\-./\\]+\.csv)', intent_original, re.IGNORECASE)
        if csv_match:
            return {"type": "csv", "file_path": csv_match.group(1)}

        # JSON file
        json_match = re.search(r'([\w\-./\\]+\.json)', intent_original, re.IGNORECASE)
        if json_match:
            return {"type": "csv", "file_path": json_match.group(1)}

        # Parquet file
        parquet_match = re.search(r'([\w\-./\\]+\.parquet)', intent_original, re.IGNORECASE)
        if parquet_match:
            return {"type": "csv", "file_path": parquet_match.group(1)}

        # Generic file reference
        file_match = re.search(r'(?:from|load|read|ingest|import)\s+([\w\-./\\]+)', intent_original)
        if file_match:
            name = file_match.group(1).strip()
            if not name.endswith(('.csv', '.json', '.parquet')):
                name += '.csv'
            return {"type": "csv", "file_path": name}

        # SQL / table reference
        if any(w in intent_lower for w in ["table", "database", "query", "select"]):
            return {"type": "db", "query": "SELECT * FROM table_name"}

        return None

    def _create_source_step(self, source: dict, intent: str, x_pos: int) -> Step:
        if source["type"] == "csv":
            return Step(
                type=StepType.CSV_SOURCE,
                label=f"Read {source['file_path']}",
                params={"file_path": source["file_path"]},
                position=NodePosition(x=x_pos, y=100),
            )
        else:
            return Step(
                type=StepType.DB_SOURCE,
                label="Query Database",
                params={"query": source.get("query", "SELECT * FROM table_name")},
                position=NodePosition(x=x_pos, y=100),
            )

    def _create_operation_step(self, op: dict, intent: str, intent_lower: str, x_pos: int) -> Step | None:
        if op["type"] == "filter":
            condition = op["hint"] if op["hint"] else "column_name IS NOT NULL"
            return Step(
                type=StepType.FILTER,
                label=f"Filter: {condition[:30]}",
                params={"condition": condition},
                position=NodePosition(x=x_pos, y=100),
            )

        elif op["type"] == "deduplicate":
            keys = [k.strip() for k in op["hint"].split(",") if k.strip()] if op["hint"] else ["id"]
            return Step(
                type=StepType.DEDUPLICATE,
                label=f"Deduplicate by {', '.join(keys)}",
                params={"key": keys, "strategy": "keep_first"},
                position=NodePosition(x=x_pos, y=100),
            )

        elif op["type"] == "aggregate":
            # Try to detect group by columns and functions
            group_by = ["order_date"] if any(w in intent_lower for w in ["daily", "by date", "per day"]) else ["category"]
            func = "SUM" if any(w in intent_lower for w in ["sum", "total", "revenue"]) else "COUNT"
            col = "amount" if any(w in intent_lower for w in ["amount", "revenue", "sales"]) else "*"
            alias = f"{func.lower()}_{col}" if col != "*" else "count"

            return Step(
                type=StepType.AGGREGATE,
                label=f"Aggregate by {', '.join(group_by)}",
                params={
                    "group_by": group_by,
                    "functions": [{"column": col, "function": func, "alias": alias}],
                },
                position=NodePosition(x=x_pos, y=100),
            )

        elif op["type"] == "transform":
            return Step(
                type=StepType.TRANSFORM,
                label="Transform",
                params={"expression": "SELECT *, CURRENT_TIMESTAMP AS processed_at FROM source_table"},
                position=NodePosition(x=x_pos, y=100),
            )

        return None

    def _create_output_step(self, intent_lower: str, x_pos: int) -> Step:
        fmt = "parquet"
        # Check for explicit output format keywords near "output/write/save/export"
        out_match = re.search(r"(?:output|write|save|export)\s+(?:to|as|in)\s+(parquet|csv|json)", intent_lower)
        if out_match:
            fmt = out_match.group(1)
        elif re.search(r"(?:to|as)\s+(parquet|csv|json)\b", intent_lower):
            fmt = re.search(r"(?:to|as)\s+(parquet|csv|json)\b", intent_lower).group(1)

        ext = fmt if fmt != "json" else "json"
        return Step(
            type=StepType.FILE_SINK,
            label=f"File Sink ({fmt.upper()})",
            params={"file_path": f"output/result.{ext}"},
            position=NodePosition(x=x_pos, y=100),
        )

    def _generate_name(self, intent: str) -> str:
        """Generate a concise pipeline name from intent."""
        # Extract meaningful nouns: file names and operation targets
        parts = []

        # Get source file name without extension
        file_match = re.search(r'([\w\-]+)\.(?:csv|json|parquet)', intent, re.IGNORECASE)
        if file_match:
            parts.append(file_match.group(1).replace("_", " ").title())

        # Get key operations
        ops = []
        if re.search(r'dedup|deduplicate|distinct|remove\s+dup', intent, re.IGNORECASE):
            ops.append("Dedup")
        if re.search(r'filter|where|only', intent, re.IGNORECASE):
            ops.append("Filter")
        if re.search(r'aggregate|group|sum|count|revenue|daily|total', intent, re.IGNORECASE):
            ops.append("Aggregate")
        if re.search(r'transform|convert|clean|normalize', intent, re.IGNORECASE):
            ops.append("Transform")
        if re.search(r'join|merge|lookup', intent, re.IGNORECASE):
            ops.append("Join")
        if re.search(r'validate|quality|check', intent, re.IGNORECASE):
            ops.append("Validate")

        if ops:
            parts.extend(ops[:2])

        if parts:
            return " ".join(parts) + " Pipeline"

        # Fallback: first 4 meaningful words
        clean = re.sub(r'[^\w\s]', '', intent)
        skip = {"load", "read", "from", "the", "and", "then", "to", "a", "an", "in", "with", "into", "output", "write", "save"}
        words = [w.capitalize() for w in clean.split() if w.lower() not in skip][:4]
        return " ".join(words) + " Pipeline" if words else "Untitled Pipeline"
