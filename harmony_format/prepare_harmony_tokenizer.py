
from transformers import AutoTokenizer

SOURCE = "gpt2"
OUTPUT = "./tokenizer_harmony"

HARMONY_TOKENS = [
    "<|start|>",
    "<|message|>",
    "<|channel|>",
    "<|end|>",
    "<|return|>",
]

tokenizer = AutoTokenizer.from_pretrained(SOURCE)

print("Original vocabulary:", len(tokenizer))

# Register the Harmony tokens.
added = tokenizer.add_special_tokens({
    "additional_special_tokens": HARMONY_TOKENS
})

print("New tokens added:", added)
print("New vocabulary:", len(tokenizer))

# Validate each token.
for token in HARMONY_TOKENS:
    ids = tokenizer.encode(
        token,
        add_special_tokens=False
    )

    assert len(ids) == 1, (
        f"{token} was split into {len(ids)} tokens: {ids}"
    )

    assert token in tokenizer.all_special_tokens, (
        f"{token} is not registered as a special token"
    )

    assert tokenizer.decode(
        ids,
        skip_special_tokens=False
    ) == token

    print(f"PASS: {token} -> {ids[0]}")

# Check the existing EOS token.
assert tokenizer.eos_token == "<|endoftext|>"
assert tokenizer.eos_token_id == 50256

# Save tokenizer.
tokenizer.save_pretrained(OUTPUT)

print("\nTokenizer prepared successfully.")
print("Saved to:", OUTPUT)
