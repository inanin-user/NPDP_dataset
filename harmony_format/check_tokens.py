from transformers import AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained(
    "./tokenizer_harmony"
)

print("Vocabulary size:", len(tokenizer))
print("EOS token:", tokenizer.eos_token)
print("EOS ID:", tokenizer.eos_token_id)

tokens = [
    "<|start|>",
    "<|message|>",
    "<|channel|>",
    "<|end|>",
    "<|return|>",
]

for token in tokens:
    ids = tokenizer.encode(
        token,
        add_special_tokens=False
    )

    print(token, ids)

    assert len(ids) == 1
    assert token in tokenizer.all_special_tokens

print("All Harmony markers validated.")