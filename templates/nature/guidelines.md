# Nature Article Writing Guidelines

Below are the instructions for writing a Nature Article in LaTeX. They summarize Nature's formatting guide
(https://www.nature.com/nature/for-authors/formatting-guide). The template uses the official Springer Nature
class `sn-jnl` with the `sn-nature` reference style.

## General Setup
* **Document Class:** `\documentclass[pdflatex,sn-nature,lineno,referee]{sn-jnl}` (keep these options: `referee`
  gives double spacing and `lineno` gives line numbers, both requested for submissions).
* **Single file:** All text (main text, references, figure legends, Methods) must be in one `.tex` file. Do not use `\input`.
* **Citations:** Use `\cite{key}` only. References are numbered in order of first appearance and are rendered by
  the class. Do not write reference numbers by hand.
* **Bibliography:** Keep `\bibliography{references}`. Do not add `\bibliographystyle`; the class sets it.
* **Tables:** Use `booktabs` (`\toprule`, `\midrule`, `\bottomrule`). Each table has a short, one-line title in bold.
  Define symbols and abbreviations immediately below the table.

## Audience and Tone (Important)
* Nature is read by scientists in **all** disciplines. Write clearly and simply; avoid jargon, and explain essential
  specialized terms briefly. Keep abbreviations, especially non-standard ones, to a minimum.
* This is NOT a machine-learning conference paper. Do not use conference conventions such as a bulleted list of
  contributions, "we beat the state of the art" framing, or a separate Related Work section.
* British spelling (Oxford English Dictionary) is preferred.

## Title
* At most **75 characters** including spaces (two lines in print).
* No numbers, acronyms, abbreviations or punctuation where avoidable. Specific enough for indexing, general enough
  for readers outside the field.

## Summary Paragraph (in `\abstract{...}`)
* Fully referenced, ideally **no more than 200 words**, separate from the main text, aimed at readers outside the discipline.
* Avoid numbers, abbreviations, acronyms and measurements unless essential.
* Structure:
  1. 2-3 sentences of basic-level introduction to the field;
  2. a brief account of the background and rationale of the work;
  3. the main conclusions, introduced by "Here we show" (or equivalent);
  4. 2-3 sentences putting the main findings into general context, showing how the field has moved forward.

## Length
* A typical 6-page Article has about **2,500 words** (summary paragraph plus main text) and 4 modest display
  items (figures/tables). A typical 8-page Article has about **4,300 words** and 5-6 display items.
* A modest display item with its legend is about a quarter page (~270 words); a composite multi-panel figure is
  about half a page (~600 words).
* Word counts exclude title, authors, acknowledgements and references. Methods are counted separately.
* Move essential but technical detail into Methods.

## Main Text
* Order: Introduction (no subheadings), Results, Discussion.
* Sections are separated by subheadings to aid navigation; **subheadings are at most 40 characters** (including spaces).
* There is **no Related Work section**: discuss prior work in the Introduction and Discussion.

## Methods
* Placed after the main text, figure legends and references in the submitted paper (the template puts it after
  Discussion; LaTeX floats handle figure placement).
* Concise but complete enough to interpret and replicate the results; typically **no more than 3,000 words**.
* Divided by short headings naming the methods; dedicated subsections for statistics are encouraged.
* **No figures or tables in Methods.**
* Do not re-describe previously published methods in detail; cite them and state any changes.
* Separate **Data availability** and **Code availability** statements are required (template end notes).

## References
* Up to about **50 references** in the main text (additional Methods references do not count toward this limit).
* Only published or accepted work, or preprints on recognized servers (for example arXiv), may be cited.

## Figures and Legends
* Each legend begins with a brief title for the whole figure, then a short description of each panel and the
  symbols used. Legends contain no methods details and are **under 300 words** each.
* All error bars and statistics must be defined in the legend.
* Figures go before the Methods section.

## End Notes (template `\bmhead` blocks, in this order)
Data availability, Code availability, Acknowledgements (brief, no effusive wording), Author contributions,
Competing interests.

## Anonymity
* Keep the template's placeholder author and affiliation. Do not invent names, affiliations, funding or acknowledgements;
  write "Acknowledgements here." style placeholders only where the materials give no information.
