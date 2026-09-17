#!/bin/sh
# Immutable public model artifacts; no package installation or executable model code.
set -eu
revision=5617a9f61b028005a4858fdac845db406aefb181
root=/data/bge-m3-$revision
mkdir -p "$root/artifacts" "$root/model"
cd "$root/artifacts"
while read -r checksum path; do
  mkdir -p "$(dirname "$path")" "$root/model/$(dirname "$path")"
  if [ ! -f "$path" ] || ! printf '%s  %s\n' "$checksum" "$path" | sha256sum -c --status; then
    # Interrupted downloads never replace a verified cached artifact.
    partial=$(mktemp "$path.partial.XXXXXX")
    trap 'rm -f "$partial"' EXIT HUP INT TERM
    curl --silent --show-error --fail --location --retry 3 --connect-timeout 20 --max-time 600 \
      "https://huggingface.co/BAAI/bge-m3/resolve/$revision/$path" --output "$partial"
    printf '%s  %s\n' "$checksum" "$partial" | sha256sum -c --status
    mv "$partial" "$path"
    trap - EXIT HUP INT TERM
  fi
  ln -sf "$root/artifacts/$path" "$root/model/$path"
done < /bootstrap/embedding-assets.sha256
# Bound CPU attention memory without truncating requests. This changes the
# accepted input limit, not the weights. Overlong inputs receive HTTP 422.
rm "$root/model/sentence_bert_config.json"
cp /bootstrap/embedding-input-limit.json "$root/model/sentence_bert_config.json"
