
#!/usr/bin/env python3
"""
pretrain_to_harmony.py

Convert synthetic pretrain.jsonl and supported public datasets
into Harmony-formatted JSONL using the existing:

    harmony_convert.py
    harmony_render.py

Output:
    {
        "id": "...",
        "source": "...",
        "task": "...",
        "training_text": "<|start|>user..."
    }

The training_text field contains the rendered Harmony conversation.
"""

import argparse
import json
import re
from pathlib import Path

import harmony_render as HR
import harmony_convert as HC


# ---------------------------------------------------------
# Utilities
# ---------------------------------------------------------

def clean(value):
    return " ".join(str(value or "").split())


def parse_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()

            if not line:
                continue

            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[WARN] Invalid JSON at line {line_no}: {exc}")


def first_match(pattern, text):
    match = re.search(pattern, text, re.I | re.M)
    return match.group(1).strip() if match else ""


# ---------------------------------------------------------
# Custom synthetic pretrain records
# ---------------------------------------------------------

def conv_pretrain_text(row):
    """
    Convert existing synthetic records:

        {
            "id": "...",
            "task": "glossary_conditioned_generation",
            "source": "llm",
            "text": "Email content...\\n\\nTerm mapping: ..."
        }

    Original email -> user
    Knowledge metadata -> assistant final
    """

    text = str(row.get("text") or "").strip()

    if not text:
        return None

    # Separate original email from appended knowledge metadata.
    marker = re.search(
        r"\n\s*Term mapping\s*:",
        text,
        re.I
    )

    if not marker:
        return None

    user_text = text[:marker.start()].strip()
    metadata = text[marker.start():].strip()

    if not user_text or not metadata:
        return None

    # Extract term mapping.
    ui_term = first_match(
        r'->\s*UI term\s*"([^"]+)"',
        metadata
    )

    field = first_match(
        r'->\s*field\s+([A-Za-z0-9_]+)',
        metadata
    )

    # Extract related database table.
    tables_text = first_match(
        r"Related tables:\s*(.+)",
        metadata
    )

    tables = [
        x.strip()
        for x in tables_text.split(",")
        if x.strip()
    ]

    # Extract related PHP files.
    files_text = first_match(
        r"Related php files:\s*(.+)",
        metadata
    )

    php_files = re.findall(
        r"([A-Za-z0-9_./-]+\.php)",
        files_text
    )

    # We need sufficient evidence to generate an answer.
    if not (ui_term and field and tables):
        return None

    table = tables[0]

    # Construct an evidence-based assistant response.
    answer = (
        f'The "{ui_term}" field is stored in column '
        f'"{field}" of table "{table}".'
    )

    if php_files:
        answer += (
            "\n\nRelated PHP files: "
            + ", ".join(php_files)
        )

    messages = [
        {
            "role": "user",
            "content": user_text
        },
        {
            "role": "assistant",
            "channel": "final",
            "content": answer
        }
    ]

    return messages


# ---------------------------------------------------------
# Dataset format detection
# ---------------------------------------------------------

def convert_row(row, analysis_prob=0.0):

    task = str(row.get("task") or "").lower()

    # 1. Existing synthetic pretrain records.
    if row.get("text") and task == "glossary_conditioned_generation":
        return conv_pretrain_text(row)

    # 2. SQuAD dataset.
    if (
        row.get("context") is not None
        and row.get("question") is not None
        and row.get("answers") is not None
    ):
        return HC.conv_squad(row)

    # 3. Structured knowledge records.
    if row.get("user_input") and (
        row.get("expected_decision")
        or task == "contrastive_term_linking"
    ):
        return HC.conv_structured(
            row,
            analysis_prob=analysis_prob
        )

    # 4. Generic chat datasets.
    if (
        isinstance(row.get("messages"), list)
        or isinstance(row.get("conversations"), list)
        or (
            row.get("instruction") is not None
            and row.get("output") is not None
        )
    ):
        return HC.conv_chat(row)

    return None


# ---------------------------------------------------------
# Main conversion
# ---------------------------------------------------------

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input",
        required=True,
        help="Input JSONL file"
    )

    parser.add_argument(
        "--output",
        required=True,
        help="Output Harmony JSONL file"
    )

    parser.add_argument(
        "--analysis-prob",
        type=float,
        default=0.0,
        help="Probability of including structured analysis messages"
    )

    parser.add_argument(
        "--save-messages",
        action="store_true",
        help="Also save the converted messages array"
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    total = 0
    converted = 0
    skipped = 0

    with open(
        output_path,
        "w",
        encoding="utf-8"
    ) as out:

        for row in parse_jsonl(input_path):

            total += 1

            messages = convert_row(
                row,
                analysis_prob=args.analysis_prob
            )

            if not messages:
                skipped += 1
                continue

            try:
                harmony_text = HR.render(
                    messages,
                    training=True
                )

            except (ValueError, KeyError) as exc:
                print(
                    f"[WARN] Skipped {row.get('id', total)}: {exc}"
                )
                skipped += 1
                continue

            result = {
                "id": row.get("id", f"row_{total}"),
                "source": row.get("source", "unknown"),
                "task": row.get("task", "chat"),
                "training_text": harmony_text
            }

            if args.save_messages:
                result["messages"] = messages

            out.write(
                json.dumps(
                    result,
                    ensure_ascii=False
                ) + "\n"
            )

            converted += 1

    print("\nConversion completed")
    print("--------------------")
    print(f"Input records : {total}")
    print(f"Converted     : {converted}")
    print(f"Skipped       : {skipped}")
    print(f"Output        : {output_path}")


if __name__ == "__main__":
    main()
