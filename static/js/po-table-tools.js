/**
 * Search filter + column header sorting for PO detail tables.
 */
function initPoTableTools(options) {
    const table = document.querySelector(options.tableId);
    const searchInput = document.querySelector(options.searchInputId);
    const rowCountEl = options.rowCountId
        ? document.querySelector(options.rowCountId)
        : null;
    const filterSelect = options.filterSelectId
        ? document.querySelector(options.filterSelectId)
        : null;

    if (!table || !searchInput) return;

    const tbody = table.querySelector('tbody');
    const headers = table.querySelectorAll('thead th.sortable');
    const rowSelector = options.rowSelector || 'tr[data-row-id]';

    let sortCol = null;
    let sortDir = 'asc';

    function getDataRows() {
        return Array.from(tbody.querySelectorAll(rowSelector));
    }

    function getSortValue(row, colIndex) {
        const cell = row.cells[colIndex];
        if (!cell) return '';
        if (cell.hasAttribute('data-sort-value')) {
            return cell.getAttribute('data-sort-value');
        }
        const input = cell.querySelector('input, select');
        if (input) {
            if (input.tagName === 'SELECT') {
                return input.options[input.selectedIndex]?.text.trim() || '';
            }
            return input.value.trim();
        }
        return cell.textContent.trim();
    }

    function compareValues(a, b, sortType) {
        if (sortType === 'number') {
            const na = parseFloat(String(a).replace(/,/g, '')) || 0;
            const nb = parseFloat(String(b).replace(/,/g, '')) || 0;
            return na - nb;
        }
        if (sortType === 'date') {
            const da = a && a !== '-' ? a : '';
            const db = b && b !== '-' ? b : '';
            if (!da && !db) return 0;
            if (!da) return 1;
            if (!db) return -1;
            return da.localeCompare(db);
        }
        return String(a).toLowerCase().localeCompare(String(b).toLowerCase());
    }

    function rowMatchesSearch(row, term) {
        if (!term) return true;
        const haystack = Array.from(row.cells).map((cell) => {
            const input = cell.querySelector('input, select');
            if (input) {
                if (input.tagName === 'SELECT') {
                    return input.options[input.selectedIndex]?.text || '';
                }
                return input.value;
            }
            return cell.textContent;
        }).join(' ').toLowerCase();
        return haystack.includes(term);
    }

    function rowMatchesFilter(row) {
        if (!filterSelect || !filterSelect.value) return true;
        const attribute = options.filterAttribute || 'filterState';
        return row.dataset[attribute] === filterSelect.value;
    }

    function updateRowCount(visible, total) {
        if (!rowCountEl) return;
        if (visible === total) {
            rowCountEl.textContent = `Showing ${total} record${total === 1 ? '' : 's'}`;
        } else {
            rowCountEl.textContent = `Showing ${visible} of ${total} records`;
        }
    }

    function renumberVisibleRows() {
        let index = 0;
        getDataRows().forEach((row) => {
            if (row.style.display === 'none') return;
            index += 1;
            const serialCell = row.cells[0];
            if (serialCell && !serialCell.querySelector('input, select, button')) {
                serialCell.textContent = index;
            }
        });
    }

    function ensureNoResultsRow() {
        let row = tbody.querySelector('tr.po-no-results');
        if (!row) {
            const colCount = table.querySelectorAll('thead th').length;
            row = document.createElement('tr');
            row.className = 'po-no-results';
            row.innerHTML = `<td colspan="${colCount}" class="text-center text-muted py-4">No matching records found.</td>`;
            tbody.appendChild(row);
        }
        return row;
    }

    function updateSortIndicators() {
        headers.forEach((th) => {
            th.classList.remove('sort-asc', 'sort-desc');
            const icon = th.querySelector('.sort-icon');
            if (icon) icon.className = 'sort-icon bi bi-arrow-down-up ms-1';
        });
        if (sortCol === null) return;
        const active = table.querySelector(`thead th.sortable[data-col="${sortCol}"]`);
        if (!active) return;
        active.classList.add(sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
        const icon = active.querySelector('.sort-icon');
        if (icon) {
            icon.className = sortDir === 'asc'
                ? 'sort-icon bi bi-sort-up ms-1'
                : 'sort-icon bi bi-sort-down ms-1';
        }
    }

    function applyTableState() {
        const term = searchInput.value.trim().toLowerCase();
        const rows = getDataRows();
        const noResultsRow = ensureNoResultsRow();
        let visibleCount = 0;

        rows.forEach((row) => {
            const match = rowMatchesSearch(row, term) && rowMatchesFilter(row);
            row.style.display = match ? '' : 'none';
            if (match) visibleCount += 1;
        });

        if (sortCol !== null) {
            const sortType = table.querySelector(`thead th.sortable[data-col="${sortCol}"]`)
                ?.getAttribute('data-sort-type') || 'text';
            const visibleRows = rows.filter((row) => row.style.display !== 'none');
            const hiddenRows = rows.filter((row) => row.style.display === 'none');

            visibleRows.sort((a, b) => {
                const cmp = compareValues(
                    getSortValue(a, sortCol),
                    getSortValue(b, sortCol),
                    sortType
                );
                return sortDir === 'asc' ? cmp : -cmp;
            });

            [...visibleRows, ...hiddenRows].forEach((row) => tbody.appendChild(row));
        }

        renumberVisibleRows();
        noResultsRow.style.display = rows.length > 0 && visibleCount === 0 ? '' : 'none';
        updateRowCount(visibleCount, rows.length);
    }

    headers.forEach((th) => {
        th.addEventListener('click', () => {
            const col = parseInt(th.getAttribute('data-col'), 10);
            if (sortCol === col) {
                sortDir = sortDir === 'asc' ? 'desc' : 'asc';
            } else {
                sortCol = col;
                sortDir = 'asc';
            }
            updateSortIndicators();
            applyTableState();
        });
    });

    searchInput.addEventListener('input', applyTableState);
    if (filterSelect) {
        filterSelect.addEventListener('change', applyTableState);
    }

    updateSortIndicators();
    applyTableState();
}
