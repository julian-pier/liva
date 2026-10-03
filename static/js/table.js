console.log("table.js loaded");

document.addEventListener("DOMContentLoaded", () => {
    const table = document.querySelector("#nutritionTable");
    if (!table) return;

    const tbody = table.querySelector("tbody");
    const addBtn = document.querySelector("#addRowBtn");

    // ---------------------------------------------------
    // ACTION BUTTONS (robust gegen Re-Renders / Live-Updates)
    // ---------------------------------------------------
    function actionsHtml() {
        // Klassen müssen zu deiner Delegation passen: edit-btn / delete-btn
        return `
            <div class="actions-flex">
                <button class="icon-btn edit-btn" title="Bearbeiten">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M12 20h9" />
                        <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
                    </svg>
                </button>
                <button class="icon-btn danger delete-btn" title="Löschen">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M3 6h18" />
                        <path d="M8 6V4h8v2" />
                        <path d="M19 6l-1 14H6L5 6" />
                        <path d="M10 11v6" />
                        <path d="M14 11v6" />
                    </svg>
                </button>
            </div>
        `;
    }

    function ensureActionButtons() {
        const rows = tbody.querySelectorAll("tr");
        rows.forEach(row => {
            // Neue Zeile (Input-Row) -> nicht anfassen
            if (row.classList.contains("new-row")) return;

            // Editing-Mode -> hat eigene Buttons
            if (row.classList.contains("editing")) return;

            const cells = row.querySelectorAll("td");
            if (cells.length < 8) return;

            const actionCell = cells[7];
            if (!actionCell) return;

            const hasEdit = actionCell.querySelector(".edit-btn");
            const hasDelete = actionCell.querySelector(".delete-btn");

            // Falls irgendwas die Action-Cell geleert hat -> wieder einsetzen
            if (!hasEdit || !hasDelete) {
                actionCell.classList.add("actions-cell");
                actionCell.innerHTML = actionsHtml();
            }
        });
    }

    // direkt beim Laden einmal sichern
    ensureActionButtons();

    // Wenn irgendein Live-Update / anderes Script DOM neu setzt:
    const mo = new MutationObserver(() => {
        ensureActionButtons();
    });
    mo.observe(tbody, { childList: true, subtree: true });

    // ---------------------------------
    // + NEUE ZEILE
    // ---------------------------------
    if (addBtn) {
        addBtn.addEventListener("click", () => {
            const row = document.createElement("tr");
            row.classList.add("new-row");

            row.innerHTML = `
                <td><input placeholder="dd.mm.yy"></td>
                <td><input></td>
                <td><input></td>
                <td><input></td>
                <td><input></td>
                <td><input></td>
                <td><input></td>
                <td class="actions-cell">
                    <button class="icon-btn save-new" title="Speichern">✔</button>
                    <button class="icon-btn danger cancel-new" title="Abbrechen">✖</button>
                </td>
            `;

            tbody.prepend(row);
        });
    }

    // ---------------------------------
    // EVENT DELEGATION
    // ---------------------------------
    tbody.addEventListener("click", async (e) => {
        const btn = e.target.closest("button");
        if (!btn) return;

        const row = btn.closest("tr");
        if (!row) return;

        // Neue Zeile abbrechen
        if (btn.classList.contains("cancel-new")) {
            row.remove();
            return;
        }

        // Neue Zeile speichern
        if (btn.classList.contains("save-new")) {
            await saveNewRow(row);
            return;
        }

        // Edit starten
        if (btn.classList.contains("edit-btn")) {
            enterEditMode(row);
            return;
        }

        // Edit speichern
        if (btn.classList.contains("save-edit")) {
            await saveEditedRow(row);
            return;
        }

        // Edit abbrechen
        if (btn.classList.contains("cancel-edit")) {
            exitEditMode(row);
            // nach Restore zur Sicherheit Buttons wieder herstellen
            ensureActionButtons();
            return;
        }

        // Löschen
        if (btn.classList.contains("delete-btn")) {
            await deleteRow(row);
            return;
        }
    });

    // ---------------------------------
    // HELFER
    // ---------------------------------
    function makePayloadFromInputs(inputs) {
        const vals = Array.from(inputs).map(i => i.value.trim());
        return {
            date: vals[0],
            weight_kg: (vals[1] || "").replace(",", "."),
            kcal: vals[2],
            protein: vals[3],
            carbs: vals[4],
            fat: vals[5],
            sugar: vals[6]
        };
    }

    // Neue Zeile speichern
    async function saveNewRow(row) {
        const inputs = row.querySelectorAll("td input");
        const payload = makePayloadFromInputs(inputs);

        const resp = await fetch(`/add_row`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (resp.ok) {
            location.reload();
        } else {
            const msg = resp.status === 403 ? "Aktion nicht erlaubt (kein Schreibzugriff)." : "Fehler beim Speichern der neuen Zeile";
            alert(msg);
        }
    }

    // Edit-Mode aktivieren
    function enterEditMode(row) {
        if (row.classList.contains("editing")) return;
        row.classList.add("editing");

        const cells = row.querySelectorAll("td");

        // Originalwerte merken
        for (let i = 0; i < 7; i++) {
            const text = cells[i].innerText.trim();
            cells[i].dataset.original = text;
            cells[i].innerHTML = `<input value="${text}">`;
        }

        // Action-Zelle sichern
        cells[7].dataset.originalHTML = cells[7].innerHTML;
        cells[7].innerHTML = `
            <div class="actions-flex">
                <button class="icon-btn save-edit" title="Speichern">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M5 13l4 4L19 7" />
                    </svg>
                </button>

                <button class="icon-btn danger cancel-edit" title="Abbrechen">
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M6 6l12 12M18 6L6 18" />
                    </svg>
                </button>
            </div>
        `;
    }

    // Edit abbrechen → Original wiederherstellen
    function exitEditMode(row) {
        const cells = row.querySelectorAll("td");

        for (let i = 0; i < 7; i++) {
            const orig = cells[i].dataset.original || "";
            cells[i].innerText = orig;
        }

        cells[7].innerHTML = cells[7].dataset.originalHTML || "";
        row.classList.remove("editing");
    }

    // Editierte Zeile speichern
    async function saveEditedRow(row) {
        const id = row.dataset.id;
        const inputs = row.querySelectorAll("td input");
        const payload = makePayloadFromInputs(inputs);

        const resp = await fetch(`/makros/update/${id}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        if (resp.ok) {
            location.reload();
        } else {
            const msg = resp.status === 403 ? "Aktion nicht erlaubt (kein Schreibzugriff)." : "Fehler beim Speichern der Änderungen";
            alert(msg);
        }
    }

    // Zeile löschen
    async function deleteRow(row) {
        const id = row.dataset.id;
        if (!id) {
            row.remove();
            return;
        }

        if (!confirm("Eintrag wirklich löschen?")) return;

        const resp = await fetch(`/api/makros/delete/${id}`, {
            method: "DELETE"
        });

        if (resp.ok) {
            row.remove();
        } else {
            const msg = resp.status === 403 ? "Aktion nicht erlaubt (kein Schreibzugriff)." : "Fehler beim Löschen";
            alert(msg);
        }
    }
});
