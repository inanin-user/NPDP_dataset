
#!/usr/bin/env python3

import argparse
from transformers import AutoTokenizer

HARMONY_TOKENS = [
    "<|start|>",
    "<|message|>",
    "<|channel|>",
    "<|end|>",
    "<|return|>",
]

OTHER_RENDERER_TOKENS = [
    "<|startoftext|>",
    "<|endoftext|>",
    "<|constrain|>",
    "<|call|>",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tokenizer",
        required=True,
        help="Hugging Face tokenizer name or local directory"
    )
    parser.add_argument(
        "--add-missing",
        action="store_true",
        help="Register missing Harmony tokens"
    )
    parser.add_argument(
        "--save-to",
        default=None,
        help="Directory to save tokenizer if tokens were added"
    )
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)

    tokens = HARMONY_TOKENS + OTHER_RENDERER_TOKENS

    print("Tokenizer:", args.tokenizer)
    print("Original vocabulary size:", len(tokenizer))
    print()

    missing = []

    for token in tokens:
        ids = tokenizer.encode(
            token,
            add_special_tokens=False
        )

        is_special = token in tokenizer.all_special_tokens

        decoded = (
            tokenizer.decode(
                ids,
                skip_special_tokens=False,
                clean_up_tokenization_spaces=False
            )
            if ids else ""
        )

        passed = (
            len(ids) == 1
            and is_special
            and decoded == token
        )

        print(f"Token: {token}")
        print(f"  IDs: {ids}")
        print(f"  Is special: {is_special}")
        print(f"  Decoded: {decoded!r}")
        print(f"  Status: {'PASS' if passed else 'FAIL'}")
        print()

        if not passed:
            missing.append(token)

    if missing and args.add_missing:
        print("Registering missing tokens...")

        tokenizer.add_special_tokens({
            "additional_special_tokens": missing
        })

        print("New vocabulary size:", len(tokenizer))

        for token in tokens:
            ids = tokenizer.encode(
                token,
                add_special_tokens=False
            )
            print(f"{token}: {ids}")

        if args.save_to:
            tokenizer.save_pretrained(args.save_to)
            print("Saved tokenizer to:", args.save_to)

    elif missing:
        print("Some tokens failed validation.")
        print("Missing or incorrectly encoded tokens:")
        for token in missing:
            print(" ", token)

        print()
        print("For a tokenizer intended for training from scratch,")
        print("consider using --add-missing and saving a new tokenizer.")

    else:
        print("All Harmony tokens passed validation.")

    # Check that the five primary Harmony tokens have distinct IDs.
    primary_ids = [
        tokenizer.encode(t, add_special_tokens=False)[0]
        for t in HARMONY_TOKENS
        if len(tokenizer.encode(t, add_special_tokens=False)) == 1
    ]

    if len(primary_ids) == len(HARMONY_TOKENS):
        print(
            "Primary token IDs are distinct:",
            len(set(primary_ids)) == len(primary_ids)
        )


if __name__ == "__main__":
    main()
