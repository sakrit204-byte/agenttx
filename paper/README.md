# Building the proposal PDF

Files:
- `agenttx-proposal.tex` — the proposal
- `refs.bib` — bibliography

There is **no LaTeX toolchain installed on this machine**, so the PDF has not
been built. Two ways to get one.

## Option A — Overleaf (fastest, nothing to install)

1. Go to overleaf.com → New Project → Upload Project
2. Upload `agenttx-proposal.tex` and `refs.bib` (zip them together first)
3. Set the compiler to **pdfLaTeX** in Menu → Settings
4. Recompile twice — BibTeX needs a second pass to resolve `\cite` keys

Overleaf has every package used here. This is the right choice if four people
are editing the paper.

## Option B — Local MiKTeX

```powershell
winget install MiKTeX.MiKTeX
```

Restart the terminal, then from this directory:

```powershell
pdflatex agenttx-proposal
bibtex   agenttx-proposal
pdflatex agenttx-proposal
pdflatex agenttx-proposal
```

Four passes is not superstition — pass 1 collects citations, `bibtex` builds the
bibliography, passes 3 and 4 resolve reference numbers and cross-references.
MiKTeX prompts to install missing packages the first time; accept them.

Everything used (`geometry`, `amsmath`, `booktabs`, `xcolor`, `graphicx`,
`hyperref`, `url`) is in a basic install.

## Before you circulate it

The document has `\todo{...}` markers in red for the institution, course code,
and the ActPlane/CRAB verification. Search for `\todo` and clear them all.

Author names are placeholders in the `\author{}` block.

## Health warning on `refs.bib`

Section A entries are verified. **Section B entries are not** — titles and arXiv
IDs came from search results, and author lists and venues are unconfirmed. Two
entries (`actplane`, `crab`) have no primary source located at all.

Open every Section B paper, complete its entry, and delete the `VERIFY` notes as
you go. Citing a paper you have not opened is how a viva goes badly.
