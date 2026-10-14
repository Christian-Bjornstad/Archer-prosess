"""MTBP alteration table extraction shared by matching and frozen capture geometry.

Hidden more-details boxes are associated by MTBP's public SmallPopup ID binding.
Reading textContent does not open the boxes or change the evidence layout.
"""

MTBP_TABLE_ROWS_JS = r"""
            tables => tables
              .filter(table => !!(table.offsetWidth || table.offsetHeight || table.getClientRects().length))
              .flatMap(table => {
                const rows = [...table.rows];
                if (!rows.length) return [];
                const headers = [...rows[0].cells].map(cell =>
                  cell.innerText.trim().replace(/\s+/g, ' ').toLowerCase());
                const columns = {
                  gene: headers.indexOf('gene'),
                  gene_info: headers.indexOf('gene info'),
                  alteration: headers.indexOf('alteration'),
                  functional_evidence: headers.findIndex(h => /^functional(?: relevance)? evidence/.test(h)),
                  biomarkers: headers.findIndex(h => h.startsWith('reported biomarker'))
                };
                if (columns.gene < 0 || columns.alteration < 0 || columns.biomarkers < 0)
                  return [];
                const accordion = table.closest('.accordion-item');
                const section = accordion?.firstElementChild?.innerText?.trim() || '';
                const spanning = new Map(), extracted = [];
                for (const row of rows.slice(1)) {
                  if (row.querySelector('th')) continue;
                  const cells = [], inherited = [...spanning.entries()];
                  for (const [column, state] of inherited) {
                    cells[column] = state.cell;
                    if (--state.remaining === 0) spanning.delete(column);
                  }
                  let column = 0;
                  for (const cell of row.cells) {
                    while (cells[column]) column++;
                    for (let offset = 0; offset < cell.colSpan; offset++) {
                      cells[column + offset] = cell;
                      if (cell.rowSpan > 1)
                        spanning.set(column + offset, {cell, remaining: cell.rowSpan - 1});
                    }
                    column += cell.colSpan;
                  }
                  const cell = field => cells[columns[field]];
                  const text = field => cell(field)?.innerText?.trim() || '';
                  const geneText = text('gene');
                  if (!geneText || !text('alteration')) continue;
                  // MTBP's SmallPopup binds each "more" trigger to an existing
                  // hidden detail box by ID. Read that exact box without opening
                  // it, so noncoding identities are available and captures stay
                  // in their original layout. Never collect other rows' details.
                  const detailText = [...cell('alteration').querySelectorAll('[data-small-popup-id]')]
                    .map(trigger => document.getElementById(
                      'small-popup-id-' + trigger.getAttribute('data-small-popup-id'))?.textContent || '')
                    .join('\n');
                  const identity = (cell('alteration')?.innerText || '') + '\n' +
                    (cell('alteration')?.textContent || '') + '\n' + detailText;
                  const rect = node => {
                    const box = node.getBoundingClientRect();
                    return {x: box.x + scrollX, y: box.y + scrollY,
                      width: box.width, height: box.height};
                  };
                  const heading = accordion?.firstElementChild;
                  const sharedGene = cell('gene').rowSpan > 1;
                  const rowBox = rect(row), geneBox = rect(cell('gene'));
                  const captureBox = sharedGene ? {
                    x: Math.min(rowBox.x, geneBox.x),
                    y: Math.min(rowBox.y, geneBox.y),
                    width: Math.max(rowBox.x + rowBox.width, geneBox.x + geneBox.width) -
                      Math.min(rowBox.x, geneBox.x),
                    height: Math.max(rowBox.y + rowBox.height, geneBox.y + geneBox.height) -
                      Math.min(rowBox.y, geneBox.y),
                  } : rowBox;
                  extracted.push({
                    row_index: [...document.querySelectorAll('table tr')].indexOf(row),
                    section,
                    gene: (geneText.match(/[A-Za-z0-9-]+/) || [''])[0],
                    gene_text: geneText,
                    gene_info: text('gene_info'),
                    alteration: text('alteration'),
                    identity_text: identity,
                    capture_geometry: {
                      gene: geneText, identity, header: rect(rows[0]), row: captureBox,
                      shared_gene_context: sharedGene,
                      section: heading && !heading.contains(table) &&
                        heading.getBoundingClientRect().height > 0 ? rect(heading) : null,
                    },
                    functional_evidence: text('functional_evidence'),
                    biomarkers: text('biomarkers'),
                    source_links: [...row.querySelectorAll('a[href]')].map(a => a.href)
                  });
                }
                return extracted;
              })
"""
