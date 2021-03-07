#!/bin/bash

# take file on stdin, ignore all whitespace, leave one word per line
# also get rid of utf-8 NBSP
get_words() {
 perl -lne 's/\xC2\xA0/ /g; foreach $x (split) { print "$x"; }' "$@"
}

for ef in ~/media/misc/evernote/*/*.stxt; do
    sf="$(echo "$ef" | perl -pe 's|/ever|/simple|; s|[.]stxt$|.txt|')";
    if [[ ! -e "$sf" ]]; then
       echo "MISSING: $ef"
       continue
    fi
    # compare in a very loose sense: ignore whitespace change and line-break
    # locations (because evernote export breaks at 80 chars!),

    # also strip first line of simplenote file and last line of evernote file,
    # where the title is
    if diff --unified \
        --label "$ef" --label "$sf" \
        <(head -n -1 "$ef" | get_words) <(tail -n +2 "$sf" | get_words); then
        echo "SAME: $ef"
    else
        echo
    fi
    # --side-by-side --suppress-common-lines
done
