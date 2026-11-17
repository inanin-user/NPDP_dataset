
#!/usr/bin/env python3
"""
standardize_dataset.py

Convert different dataset formats into a common internal format.

Supported:
1. Custom synthetic JSON / JSONL
2. SQuAD JSON / JSONL

Output:
common_dataset_hf/
    custom_synthetic.jsonl
    squad.jsonl
    conversion_report.json
"""

import json
import argparse
from pathlib import Path
from collections import Counter


OUTPUT_DIR = Path("common_dataset_hf")


def read_dataset(file_path):
    """Read JSON or JSONL dataset."""

    file_path = Path(file_path)

    if file_path.suffix.lower() == ".jsonl":
        records = []

        with file_path.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, 1):
                if not line.strip():
                    continue

                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"[WARNING] Invalid JSONL line {line_number}: {e}")

        return records

    with file_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # SQuAD dataset structure
    if isinstance(data, dict) and "data" in data:
        return data["data"]

    return data if isinstance(data, list) else [data]


def convert_custom_synthetic(records):
    """Convert custom synthetic dataset."""

    converted = []

    for index, record in enumerate(records, 1):

        if not isinstance(record, dict):
            continue

        training_text = record.get("text")

        if not training_text:
            continue

        converted.append({
            "id": str(record.get("id") or f"custom_{index:06d}"),
            "source": "custom_synthetic",
            "task": record.get("task", "text_generation"),
            "training_text": str(training_text)
        })

    return converted


def convert_squad(records):
    """Convert SQuAD dataset into one record per QA pair."""

    converted = []
    counter = 0

    for article in records:

        title = article.get("title", "Unknown")

        for paragraph in article.get("paragraphs", []):

            context = paragraph.get("context", "")

            for qa in paragraph.get("qas", []):

                counter += 1

                question = qa.get("question", "")
                answers = qa.get("answers", [])

                # Use the first available answer.
                answer = (
                    answers[0].get("text", "")
                    if answers else ""
                )

                training_text = (
                    f"Article: {title}\n\n"
                    f"Context:\n{context}\n\n"
                    f"Question:\n{question}\n\n"
                    f"Answer:\n{answer}"
                )

                converted.append({
                    "id": f"squad_{counter:06d}",
                    "source": "squad",
                    "task": "question_answering",
                    "training_text": training_text
                })

    return converted


def write_jsonl(records, output_path):

    with output_path.open("w", encoding="utf-8") as f:

        for record in records:
            f.write(
                json.dumps(record, ensure_ascii=False) + "\n"
            )


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--custom",
        type=str,
        help="Custom synthetic JSON/JSONL file"
    )

    parser.add_argument(
        "--squad",
        type=str,
        help="SQuAD JSON/JSONL file"
    )

    parser.add_argument(
        "--output",
        type=str,
        default=str(OUTPUT_DIR)
    )

    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "output_directory": str(output_dir),
        "datasets": {}
    }

    # Custom synthetic dataset
    if args.custom:

        records = read_dataset(args.custom)
        converted = convert_custom_synthetic(records)

        output_path = output_dir / "custom_synthetic.jsonl"

        write_jsonl(converted, output_path)

        report["datasets"]["custom_synthetic"] = {
            "input_records": len(records),
            "output_records": len(converted),
            "output_file": output_path.name
        }

        print(f"[OK] Custom synthetic: {len(converted)} records")

    # SQuAD dataset
    if args.squad:

        records = read_dataset(args.squad)
        converted = convert_squad(records)

        output_path = output_dir / "squad.jsonl"

        write_jsonl(converted, output_path)

        report["datasets"]["squad"] = {
            "input_articles": len(records),
            "output_records": len(converted),
            "output_file": output_path.name
        }

        print(f"[OK] SQuAD: {len(converted)} records")

    # Conversion report
    with (output_dir / "conversion_report.json").open(
        "w", encoding="utf-8"
    ) as f:
        json.dump(report, f, indent=4, ensure_ascii=False)

    print("\nConversion completed.")
    print(f"Output directory: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
