(function () {
  if (typeof Element !== "undefined") {
    if (!Element.prototype.matches) {
      Element.prototype.matches = Element.prototype.msMatchesSelector || Element.prototype.webkitMatchesSelector;
    }
    if (!Element.prototype.closest) {
      Element.prototype.closest = function (selector) {
        var el = this;
        while (el && el.nodeType === 1) {
          if (el.matches(selector)) return el;
          el = el.parentElement || el.parentNode;
        }
        return null;
      };
    }
  }

  function actionIcon(name, filled) {
    if (name === "delete") {
      return '<svg class="physique-action-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
        '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 10v6M14 10v6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path>' +
        '</svg>';
    }
    return '<svg class="physique-action-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
      '<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78L12 21.23l8.84-8.84a5.5 5.5 0 0 0 0-7.78Z" fill="' + (filled ? "currentColor" : "none") + '" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path>' +
      '</svg>';
  }

  function init() {
    var page = document.querySelector(".physique-page");
    if (!page) return;
    document.body.classList.remove("physique-no-scroll");

    var csrfToken = page.getAttribute("data-csrf-token") || "";
    var form = document.getElementById("physique-upload-form");
    var feed = document.getElementById("physique-feed");
    var countLabel = document.getElementById("physique-count-label");
    var statusEl = document.getElementById("physique-upload-status");
    var fileInput = document.getElementById("physique-file-input");
    var dropTitle = document.querySelector(".physique-drop-title");
    var filterForm = document.getElementById("physique-filter-form");
    var filterView = document.getElementById("physique-filter-view");
    var filterSort = document.getElementById("physique-filter-sort");
    var compareSelectButton = document.getElementById("physique-compare-select");
    var selectionBar = document.getElementById("physique-selection-bar");
    var selectionStatus = document.getElementById("physique-selection-status");
    var selectionOpenButton = selectionBar ? selectionBar.querySelector("[data-selection-open='1']") : null;

    var viewer = document.getElementById("physique-viewer");
    if (!viewer) {
      var w = document.createElement("div");
      w.id = "physique-viewer";
      w.className = "physique-viewer";
      w.setAttribute("hidden", "hidden");
      w.innerHTML = '' +
        '<div class="physique-viewer-backdrop" data-viewer-close="1"></div>' +
        '<div class="physique-viewer-shell">' +
        '  <div class="physique-viewer-topbar">' +
        '    <button type="button" class="physique-viewer-round-btn" data-viewer-close="1" aria-label="Vollbild schließen">×</button>' +
        '    <span id="physique-viewer-counter" class="physique-viewer-counter"></span>' +
        '    <div class="physique-viewer-actions" hidden>' +
        '      <button type="button" class="physique-viewer-action physique-delete-btn" data-viewer-delete="1" aria-label="Bild löschen"></button>' +
        '      <button type="button" class="physique-viewer-action physique-like-btn" data-viewer-like="1" aria-label="Als Favorit markieren"></button>' +
        '    </div>' +
        '  </div>' +
        '  <div class="physique-viewer-grid">' +
        '    <button type="button" class="physique-viewer-pane is-active" data-pane-index="0">' +
        '      <img id="physique-viewer-img-0" src="" alt="Physique Vollbild links">' +
        '      <span id="physique-viewer-meta-0" class="physique-viewer-meta"></span>' +
        '    </button>' +
        '    <button type="button" class="physique-viewer-pane" data-pane-index="1">' +
        '      <img id="physique-viewer-img-1" src="" alt="Physique Vollbild rechts">' +
        '      <span id="physique-viewer-meta-1" class="physique-viewer-meta"></span>' +
        '    </button>' +
        '  </div>' +
        '  <div class="physique-viewer-controls" aria-label="Galerie-Steuerung">' +
        '    <button type="button" class="physique-viewer-control" data-viewer-prev="1" aria-label="Vorheriges Bild">‹</button>' +
        '    <button type="button" class="physique-viewer-control physique-viewer-compare" data-viewer-compare="1">Vergleichen</button>' +
        '    <button type="button" class="physique-viewer-control" data-viewer-next="1" aria-label="Nächstes Bild">›</button>' +
        '  </div>' +
        '  <div class="physique-viewer-hint">Links/rechts wischen: wechseln · hoch wischen: vergleichen · runter wischen: Vergleich schließen</div>' +
        '</div>';
      page.appendChild(w);
      viewer = w;
    }
    if (viewer.parentElement !== document.body) {
      document.body.appendChild(viewer);
    }

    var paneEls = Array.prototype.slice.call(viewer.querySelectorAll(".physique-viewer-pane"));
    var imgEls = [
      viewer.querySelector("#physique-viewer-img-0"),
      viewer.querySelector("#physique-viewer-img-1"),
    ];
    var metaEls = [
      viewer.querySelector("#physique-viewer-meta-0"),
      viewer.querySelector("#physique-viewer-meta-1"),
    ];
    var viewerCounter = viewer.querySelector("#physique-viewer-counter");
    var viewerCompareButton = viewer.querySelector("[data-viewer-compare='1']");
    var viewerActionGroup = viewer.querySelector(".physique-viewer-actions");
    var viewerLikeButton = viewer.querySelector("[data-viewer-like='1']");
    var viewerDeleteButton = viewer.querySelector("[data-viewer-delete='1']");

    var state = {
      rawRows: [],
      rows: [],
      assets: [],
      viewerOpen: false,
      viewerIndices: [],
      activePane: 0,
      compareSelecting: false,
      compareAssetIds: [],
      zoom: [
        { scale: 1, x: 0, y: 0 },
        { scale: 1, x: 0, y: 0 },
      ],
    };
    var thumbObserver = null;

    function activateVisibleThumbnails() {
      if (thumbObserver) {
        thumbObserver.disconnect();
        thumbObserver = null;
      }
      var images = Array.prototype.slice.call(feed.querySelectorAll("img[data-src]"));
      function loadImage(image) {
        var src = image.getAttribute("data-src");
        if (!src) return;
        image.src = src;
        image.removeAttribute("data-src");
      }
      if (!("IntersectionObserver" in window)) {
        images.forEach(loadImage);
        return;
      }
      thumbObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          loadImage(entry.target);
          thumbObserver.unobserve(entry.target);
        });
      }, { root: null, rootMargin: "320px 0px", threshold: 0.01 });
      images.forEach(function (image) { thumbObserver.observe(image); });
    }

    function esc(text) {
      return String(text || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function formatDate(iso) {
      if (!iso) return "-";
      var parsed = new Date(iso);
      if (isNaN(parsed.getTime())) return iso;
      return parsed.toLocaleString("de-DE", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    }

    function normalizeRows(rows) {
      return (Array.isArray(rows) ? rows : []).map(function (row) {
        var captured = String((row && (row.captured_at || row.created_at)) || "");
        var parsed = new Date(captured);
        row._ts = isNaN(parsed.getTime()) ? 0 : parsed.getTime();
        row._formatted_date = formatDate(captured);
        return row;
      });
    }

    function chipHtml(row, type, value, emptyLabel) {
      var hasValue = value != null && String(value).trim() !== "";
      var label = hasValue ? String(value) : emptyLabel;
      var cls = hasValue ? "physique-chip" : "physique-chip is-empty";
      return '<button type="button" class="' + cls + '" data-chip-edit="1" data-update-id="' + Number(row.id) + '" data-tag-type="' + esc(type) + '">' + esc(label) + '</button>';
    }

    function buildAssets(rows) {
      var out = [];
      (rows || []).forEach(function (row) {
        var assets = Array.isArray(row.assets) ? row.assets : [];
        assets.forEach(function (asset, assetIndex) {
          out.push({
            id: Number(asset.id),
            updateId: Number(row.id),
            assetIndex: assetIndex,
            url: String(asset.url || ""),
            originalUrl: String(asset.original_url || asset.url || ""),
            width: Number(asset.width) || null,
            height: Number(asset.height) || null,
            capturedAt: asset.captured_at || row.captured_at || row.created_at || "",
            poseLabel: row.pose_label || "",
            weightKg: Object.prototype.hasOwnProperty.call(asset, "weight_kg") ? asset.weight_kg : row.weight_kg,
            note: row.note || "",
            liked: !!asset.liked,
          });
        });
      });
      return out;
    }

    function hasLikedAsset(row) {
      var assets = Array.isArray(row.assets) ? row.assets : [];
      return assets.some(function (a) { return !!a.liked; });
    }

    function getFilters() {
      return {
        view: filterView ? String(filterView.value || "").trim() : "",
        sort: filterSort ? String(filterSort.value || "date_desc").trim() : "date_desc",
      };
    }

    function sortRows(rows, sortMode) {
      var copy = (rows || []).slice();
      function rowTs(row) {
        return Number(row && row._ts) || 0;
      }
      function rowWeight(row) {
        return row.weight_kg == null ? null : Number(row.weight_kg);
      }
      copy.sort(function (a, b) {
        if (sortMode === "date_asc") return rowTs(a) - rowTs(b);
        if (sortMode === "weight_desc") {
          var wa = rowWeight(a);
          var wb = rowWeight(b);
          if (wa == null && wb == null) return rowTs(b) - rowTs(a);
          if (wa == null) return 1;
          if (wb == null) return -1;
          if (wb !== wa) return wb - wa;
          return rowTs(b) - rowTs(a);
        }
        if (sortMode === "weight_asc") {
          var wa2 = rowWeight(a);
          var wb2 = rowWeight(b);
          if (wa2 == null && wb2 == null) return rowTs(b) - rowTs(a);
          if (wa2 == null) return 1;
          if (wb2 == null) return -1;
          if (wa2 !== wb2) return wa2 - wb2;
          return rowTs(b) - rowTs(a);
        }
        if (sortMode === "liked_first") {
          var la = hasLikedAsset(a) ? 1 : 0;
          var lb = hasLikedAsset(b) ? 1 : 0;
          if (lb !== la) return lb - la;
          return rowTs(b) - rowTs(a);
        }
        return rowTs(b) - rowTs(a);
      });
      return copy;
    }

    function filteredRows(rows) {
      var f = getFilters();
      var filtered = (rows || []).filter(function (row) {
        if (f.view && String(row.pose_label || "") !== f.view) return false;
        return true;
      });
      return sortRows(filtered, f.sort);
    }

    function hydratePoseFilterOptions(rows) {
      if (!filterView) return;
      var current = String(filterView.value || "");
      var poses = new Set();
      (rows || []).forEach(function (row) {
        var pose = String(row.pose_label || "").trim();
        if (pose) poses.add(pose);
      });
      var options = ['<option value="">Alle</option>'];
      Array.from(poses).sort().forEach(function (pose) {
        options.push('<option value="' + esc(pose) + '">' + esc(pose) + '</option>');
      });
      filterView.innerHTML = options.join("");
      if (current && poses.has(current)) filterView.value = current;
    }

    function renderWithFilters() {
      hydratePoseFilterOptions(state.rawRows);
      renderFeed(filteredRows(state.rawRows));
    }

    function sortAssets(assets, sortMode) {
      var copy = (assets || []).slice();
      copy.sort(function (a, b) {
        var ta = new Date(a.capturedAt || 0).getTime() || 0;
        var tb = new Date(b.capturedAt || 0).getTime() || 0;
        if (sortMode === "date_asc") return ta - tb || a.id - b.id;
        if (sortMode === "liked_first" && Number(b.liked) !== Number(a.liked)) {
          return Number(b.liked) - Number(a.liked);
        }
        if (sortMode === "weight_desc" || sortMode === "weight_asc") {
          var wa = a.weightKg == null ? null : Number(a.weightKg);
          var wb = b.weightKg == null ? null : Number(b.weightKg);
          if (wa != null && wb != null && wa !== wb) return sortMode === "weight_asc" ? wa - wb : wb - wa;
          if (wa == null && wb != null) return 1;
          if (wa != null && wb == null) return -1;
        }
        return tb - ta || b.id - a.id;
      });
      return copy;
    }

    function updateSelectionUi() {
      var count = state.compareAssetIds.length;
      if (selectionBar) selectionBar.hidden = !state.compareSelecting;
      if (compareSelectButton) {
        compareSelectButton.classList.toggle("is-active", state.compareSelecting);
        compareSelectButton.textContent = state.compareSelecting ? "Auswahl läuft" : "2 Fotos vergleichen";
      }
      if (selectionStatus) {
        selectionStatus.textContent = count === 0
          ? "Erstes Foto auswählen"
          : (count === 1 ? "Erstes gewählt · zweites Foto suchen" : "2 Fotos gewählt");
      }
      if (selectionOpenButton) selectionOpenButton.disabled = count !== 2;
      if (feed) {
        Array.prototype.forEach.call(feed.querySelectorAll(".physique-photo[data-asset-id]"), function (tile) {
          var selected = state.compareAssetIds.indexOf(Number(tile.getAttribute("data-asset-id") || 0)) >= 0;
          tile.classList.toggle("is-selected", selected);
        });
      }
    }

    function startCompareSelection() {
      state.compareSelecting = true;
      state.compareAssetIds = [];
      updateSelectionUi();
    }

    function cancelCompareSelection() {
      state.compareSelecting = false;
      state.compareAssetIds = [];
      updateSelectionUi();
    }

    function toggleCompareAsset(assetId) {
      var id = Number(assetId) || 0;
      if (!id) return;
      var pos = state.compareAssetIds.indexOf(id);
      if (pos >= 0) state.compareAssetIds.splice(pos, 1);
      else if (state.compareAssetIds.length < 2) state.compareAssetIds.push(id);
      else state.compareAssetIds[1] = id;
      updateSelectionUi();
      if (state.compareAssetIds.length === 2) openSelectedComparison();
    }

    function openSelectedComparison() {
      if (state.compareAssetIds.length !== 2) return;
      var selectedIds = state.compareAssetIds.slice();
      var allAssets = sortAssets(buildAssets(state.rawRows), getFilters().sort);
      var first = allAssets.findIndex(function (asset) { return Number(asset.id) === selectedIds[0]; });
      var second = allAssets.findIndex(function (asset) { return Number(asset.id) === selectedIds[1]; });
      if (first < 0 || second < 0) return;
      state.assets = allAssets;
      state.compareSelecting = false;
      state.compareAssetIds = [];
      updateSelectionUi();
      openViewer(first, second);
    }

    function renderFeed(rows) {
      rows = Array.isArray(rows) ? rows : [];
      state.rows = rows;
      state.assets = sortAssets(buildAssets(rows), getFilters().sort);
      var totalAssets = buildAssets(state.rawRows).length;
      countLabel.textContent = state.assets.length + " / " + totalAssets + " Fotos";

      if (!state.assets.length) {
        feed.innerHTML = '<div class="physique-empty">Noch keine Physique-Fotos gespeichert.</div>';
        closeViewer();
        return;
      }

      feed.innerHTML = state.assets.map(function (asset, globalIndex) {
        var likedClass = asset.liked ? " is-liked" : "";
        var width = Math.max(1, Number(asset.width) || 4);
        var height = Math.max(1, Number(asset.height) || 5);
        var weight = asset.weightKg == null ? "" : (String(asset.weightKg).replace(".", ",") + " kg");
        var meta = [asset.poseLabel, weight].filter(Boolean).join(" · ");
        return '' +
          '<article class="physique-photo" data-asset-id="' + Number(asset.id) + '" data-asset-index="' + globalIndex + '">' +
          '  <button type="button" class="physique-thumb" data-open-detail="1" data-asset-index="' + globalIndex + '" aria-label="Foto öffnen">' +
          '    <img data-src="' + esc(asset.url) + '" alt="Physique Foto" decoding="async" fetchpriority="low" width="' + width + '" height="' + height + '">' +
          '    <span class="physique-photo-gradient"></span>' +
          '    <span class="physique-photo-selected" aria-hidden="true">✓</span>' +
          '    <span class="physique-photo-date">' + esc(formatDate(asset.capturedAt)) + '</span>' +
          (meta ? ('<span class="physique-photo-meta">' + esc(meta) + '</span>') : '') +
          '  </button>' +
          '  <div class="physique-photo-actions">' +
          '    <button type="button" class="physique-delete-btn" data-asset-delete="1" data-asset-id="' + Number(asset.id) + '" aria-label="Bild löschen">' + actionIcon("delete") + '</button>' +
          '    <button type="button" class="physique-like-btn' + likedClass + '" data-like-toggle="1" data-asset-id="' + Number(asset.id) + '" aria-label="Favorit">' + actionIcon("like", asset.liked) + '</button>' +
          '  </div>' +
          '</article>';
      }).join("");
      activateVisibleThumbnails();
      updateSelectionUi();
    }

    function resetZoom(paneIndex) {
      state.zoom[paneIndex] = { scale: 1, x: 0, y: 0 };
    }

    function applyZoom(paneIndex) {
      var image = imgEls[paneIndex];
      var zoom = state.zoom[paneIndex];
      if (!image || !zoom) return;
      image.style.transform = "translate3d(" + zoom.x + "px," + zoom.y + "px,0) scale(" + zoom.scale + ")";
      image.style.transformOrigin = "50% 50%";
      paneEls[paneIndex].classList.toggle("is-zoomed", zoom.scale > 1.01);
    }

    function renderViewer() {
      if (!state.viewerOpen) return;
      var count = state.assets.length;
      if (!count) {
        closeViewer();
        return;
      }

      var i0 = Number(state.viewerIndices[0]);
      if (!isFinite(i0) || i0 < 0 || i0 >= count) i0 = 0;
      var hasSplit = state.viewerIndices.length > 1;
      var i1 = Number(state.viewerIndices[1]);
      if (hasSplit && (!isFinite(i1) || i1 < 0 || i1 >= count)) i1 = (i0 + 1) % count;

      state.viewerIndices = hasSplit ? [i0, i1] : [i0];
      viewer.hidden = false;
      viewer.removeAttribute("hidden");
      viewer.style.display = "block";
      viewer.style.position = "fixed";
      viewer.style.left = "0";
      viewer.style.top = "0";
      viewer.style.right = "0";
      viewer.style.bottom = "0";
      viewer.style.zIndex = "2147483647";
      viewer.style.pointerEvents = "auto";
      viewer.classList.add("is-open");
      viewer.classList.toggle("is-split", hasSplit);

      var left = state.assets[i0];
      imgEls[0].src = left.url;
      metaEls[0].textContent = [formatDate(left.capturedAt), left.poseLabel || "", left.weightKg != null ? (String(left.weightKg).replace(".", ",") + " kg") : ""]
        .filter(Boolean).join(" · ");
      paneEls[0].classList.remove("is-hidden");

      if (viewerActionGroup) viewerActionGroup.hidden = hasSplit;
      if (viewerLikeButton) {
        viewerLikeButton.setAttribute("data-asset-id", String(left.id));
        viewerLikeButton.classList.toggle("is-liked", !!left.liked);
        viewerLikeButton.setAttribute("aria-label", left.liked ? "Favorit entfernen" : "Als Favorit markieren");
        viewerLikeButton.innerHTML = actionIcon("like", !!left.liked);
      }
      if (viewerDeleteButton) {
        viewerDeleteButton.setAttribute("data-asset-id", String(left.id));
        viewerDeleteButton.innerHTML = actionIcon("delete");
      }

      if (hasSplit) {
        var right = state.assets[i1];
        imgEls[1].src = right.url;
        metaEls[1].textContent = [formatDate(right.capturedAt), right.poseLabel || "", right.weightKg != null ? (String(right.weightKg).replace(".", ",") + " kg") : ""]
          .filter(Boolean).join(" · ");
        paneEls[1].classList.remove("is-hidden");
      } else {
        paneEls[1].classList.add("is-hidden");
      }

      paneEls[0].classList.toggle("is-active", state.activePane === 0);
      paneEls[1].classList.toggle("is-active", state.activePane === 1);
      applyZoom(0);
      applyZoom(1);
      if (viewerCounter) {
        viewerCounter.textContent = hasSplit
          ? ((i0 + 1) + " / " + count + "  ·  " + (i1 + 1) + " / " + count)
          : ((i0 + 1) + " / " + count);
      }
      if (viewerCompareButton) {
        viewerCompareButton.textContent = hasSplit ? "Vergleich schließen" : "Vergleichen";
        viewerCompareButton.classList.toggle("is-active", hasSplit);
      }
    }

    function openViewer(indexA, indexB) {
      var count = state.assets.length;
      if (!count) return;
      var a = Math.max(0, Math.min(Number(indexA) || 0, count - 1));
      if (indexB == null) {
        state.viewerIndices = [a];
        state.activePane = 0;
      } else {
        var b = Math.max(0, Math.min(Number(indexB) || 0, count - 1));
        state.viewerIndices = [a, b];
        state.activePane = 1;
      }
      resetZoom(0);
      resetZoom(1);
      state.viewerOpen = true;
      renderViewer();
    }

    function closeViewer() {
      state.viewerOpen = false;
      state.viewerIndices = [];
      state.activePane = 0;
      viewer.classList.remove("is-open", "is-split");
      viewer.hidden = true;
      viewer.style.display = "none";
      document.body.classList.remove("physique-no-scroll");
      imgEls.forEach(function (image) { if (image) image.removeAttribute("src"); });
    }

    function moveActive(step) {
      if (!state.viewerOpen || !state.assets.length) return;
      var paneIndex = state.viewerIndices.length > 1 ? state.activePane : 0;
      var current = Number(state.viewerIndices[paneIndex]) || 0;
      var next = current + step;
      if (next < 0) next = state.assets.length - 1;
      if (next >= state.assets.length) next = 0;
      state.viewerIndices[paneIndex] = next;
      resetZoom(paneIndex);
      renderViewer();
    }

    function splitFromSingle() {
      if (!state.viewerOpen || state.viewerIndices.length !== 1 || state.assets.length < 2) return;
      var a = Number(state.viewerIndices[0]) || 0;
      var b = (a + 1) % state.assets.length;
      state.viewerIndices = [a, b];
      state.activePane = 1;
      resetZoom(1);
      renderViewer();
    }

    function exitSplit() {
      if (!state.viewerOpen || state.viewerIndices.length < 2) return;
      var keep = Number(state.viewerIndices[state.activePane]) || 0;
      state.viewerIndices = [keep];
      state.activePane = 0;
      resetZoom(0);
      resetZoom(1);
      renderViewer();
    }

    window.physiqueOpenDetail = function (event, idx) {
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      var index = Number(idx);
      if (!isFinite(index) || index < 0) return false;
      if (!state.viewerOpen) {
        openViewer(index);
        return false;
      }
      if (state.viewerIndices.length === 1) {
        var first = Number(state.viewerIndices[0]) || 0;
        if (first === index) openViewer(index);
        else openViewer(first, index);
        return false;
      }
      state.viewerIndices[state.activePane] = index;
      renderViewer();
      return false;
    };

    function saveTag(row, tagType, rawValue) {
      var payload = {
        pose_label: String(row.pose_label || "").trim(),
        weight_kg: row.weight_kg == null ? null : row.weight_kg,
      };
      if (tagType === "weight_kg") {
        var value = String(rawValue || "").trim();
        payload.weight_kg = value === "" ? null : value.replace(",", ".");
      } else {
        payload[tagType] = String(rawValue || "").trim();
      }

      return fetch("/api/physique/updates/" + Number(row.id), {
        method: "PATCH",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify(payload),
      }).then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok || !data.ok) throw new Error(data.error || "save_failed");
          return data.update || {};
        });
      });
    }

    function setAssetLiked(assetId, liked) {
      return fetch("/api/physique/assets/" + Number(assetId) + "/favorite", {
        method: "PATCH",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({ liked: !!liked }),
      }).then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok || !data.ok) throw new Error(data.error || "favorite_failed");
          return data.asset || {};
        });
      });
    }

    function deleteAsset(assetId) {
      return fetch("/api/physique/assets/" + Number(assetId), {
        method: "DELETE",
        credentials: "same-origin",
        headers: {
          "X-CSRF-Token": csrfToken,
          "X-Requested-With": "XMLHttpRequest",
        },
      }).then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok || !data.ok) throw new Error(data.error || "delete_failed");
          return data.asset || {};
        });
      });
    }

    function applyAssetLikePatch(assetPatch) {
      var id = Number(assetPatch.id);
      var liked = !!assetPatch.liked;
      if (!id) return;
      state.rawRows.forEach(function (row) {
        var assets = Array.isArray(row.assets) ? row.assets : [];
        assets.forEach(function (asset) {
          if (Number(asset.id) === id) asset.liked = liked;
        });
      });
      state.rows.forEach(function (row) {
        var assets = Array.isArray(row.assets) ? row.assets : [];
        assets.forEach(function (asset) {
          if (Number(asset.id) === id) asset.liked = liked;
        });
      });
      state.assets.forEach(function (asset) {
        if (Number(asset.id) === id) asset.liked = liked;
      });
    }

    function applyRowPatch(patch) {
      var id = Number(patch.id);
      var row = state.rawRows.find(function (item) { return Number(item.id) === id; });
      if (!row) return;
      if (Object.prototype.hasOwnProperty.call(patch, "pose_label")) row.pose_label = patch.pose_label || "";
      if (Object.prototype.hasOwnProperty.call(patch, "weight_kg")) row.weight_kg = patch.weight_kg == null ? null : patch.weight_kg;
    }

    function loadFeed() {
      return fetch("/api/physique/updates", {
        cache: "no-store",
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      }).then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok || !data.ok) throw new Error(data.error || "load_failed");
          state.rawRows = normalizeRows(data.updates);
          renderWithFilters();
        });
      });
    }

    if (fileInput) {
      fileInput.addEventListener("change", function () {
        var count = (fileInput.files && fileInput.files.length) || 0;
        if (dropTitle) dropTitle.textContent = count ? (count + " Bild" + (count === 1 ? "" : "er") + " gewählt") : "Bilder wählen";
      });
    }

    if (feed) {
      feed.addEventListener("click", function (event) {
        var target = event.target instanceof Element ? event.target : null;
        if (!target) return;

        var chip = target.closest("[data-chip-edit='1']");
        if (chip) {
          event.preventDefault();
          var updateId = Number(chip.getAttribute("data-update-id") || 0);
          var tagType = String(chip.getAttribute("data-tag-type") || "");
          if (!updateId || !tagType) return;

          var row = state.rows.find(function (item) { return Number(item.id) === updateId; });
          if (!row) return;

          var promptText = "Neuer Wert:";
          if (tagType === "pose_label") promptText = "Pose (leer = entfernen):";
          if (tagType === "weight_kg") promptText = "Gewicht in kg (leer = entfernen):";
          var current = chip.textContent || "";
          var next = window.prompt(promptText, current.replace(" kg", ""));
          if (next == null) return;

          saveTag(row, tagType, next)
            .then(function (patch) {
              applyRowPatch(patch);
              renderWithFilters();
            })
            .catch(function (error) {
              window.alert("Tag konnte nicht gespeichert werden: " + (error.message || error));
            });
          return;
        }

        var likeBtn = target.closest("[data-like-toggle='1']");
        if (likeBtn) {
          event.preventDefault();
          event.stopPropagation();
          var assetId = Number(likeBtn.getAttribute("data-asset-id") || 0);
          if (!assetId) return;
          var isLiked = likeBtn.classList.contains("is-liked");
          setAssetLiked(assetId, !isLiked)
            .then(function (patch) {
              applyAssetLikePatch(patch);
              renderWithFilters();
              if (state.viewerOpen) renderViewer();
            })
            .catch(function (error) {
              window.alert("Favorit konnte nicht gespeichert werden: " + (error.message || error));
            });
          return;
        }

        var deleteBtn = target.closest("[data-asset-delete='1']");
        if (deleteBtn) {
          event.preventDefault();
          event.stopPropagation();
          var deleteAssetId = Number(deleteBtn.getAttribute("data-asset-id") || 0);
          if (!deleteAssetId) return;
          var ok = window.confirm("Bild wirklich löschen?");
          if (!ok) return;
          deleteAsset(deleteAssetId)
            .then(function () {
              closeViewer();
              return loadFeed();
            })
            .catch(function (error) {
              window.alert("Bild konnte nicht gelöscht werden: " + (error.message || error));
            });
          return;
        }

        var detailBtn = target.closest("button[data-open-detail='1']");
        if (!detailBtn) return;
        event.preventDefault();

        var idx = Number(detailBtn.getAttribute("data-asset-index"));
        if (state.compareSelecting) {
          var selectedAsset = state.assets[idx];
          if (selectedAsset) toggleCompareAsset(selectedAsset.id);
          return;
        }
        window.physiqueOpenDetail(event, idx);
      });
    }

    if (compareSelectButton) {
      compareSelectButton.addEventListener("click", function () {
        if (state.compareSelecting) cancelCompareSelection();
        else startCompareSelection();
      });
    }

    if (selectionBar) {
      selectionBar.addEventListener("click", function (event) {
        var target = event.target instanceof Element ? event.target : null;
        if (!target) return;
        if (target.closest("[data-selection-cancel='1']")) cancelCompareSelection();
        if (target.closest("[data-selection-open='1']")) openSelectedComparison();
      });
    }

    if (filterForm) {
      filterForm.addEventListener("input", function () {
        renderWithFilters();
      });
      filterForm.addEventListener("change", function () {
        renderWithFilters();
      });
    }
    if (filterSort && !filterSort.value) filterSort.value = "date_desc";

    viewer.addEventListener("click", function (event) {
      var target = event.target instanceof Element ? event.target : null;
      if (!target) return;

      if (target.closest("[data-viewer-close='1']")) {
        event.preventDefault();
        closeViewer();
        return;
      }

      var viewerLike = target.closest("[data-viewer-like='1']");
      if (viewerLike) {
        event.preventDefault();
        event.stopPropagation();
        var likedAssetId = Number(viewerLike.getAttribute("data-asset-id") || 0);
        var likedAsset = state.assets.find(function (asset) { return Number(asset.id) === likedAssetId; });
        if (!likedAssetId || !likedAsset) return;
        setAssetLiked(likedAssetId, !likedAsset.liked)
          .then(function (patch) {
            applyAssetLikePatch(patch);
            renderWithFilters();
            var currentIndex = state.assets.findIndex(function (asset) { return Number(asset.id) === likedAssetId; });
            if (currentIndex < 0) {
              closeViewer();
              return;
            }
            state.viewerIndices = [currentIndex];
            state.activePane = 0;
            renderViewer();
          })
          .catch(function (error) {
            window.alert("Favorit konnte nicht gespeichert werden: " + (error.message || error));
          });
        return;
      }

      var viewerDelete = target.closest("[data-viewer-delete='1']");
      if (viewerDelete) {
        event.preventDefault();
        event.stopPropagation();
        var viewerAssetId = Number(viewerDelete.getAttribute("data-asset-id") || 0);
        if (!viewerAssetId || !window.confirm("Bild wirklich löschen?")) return;
        deleteAsset(viewerAssetId)
          .then(function () {
            closeViewer();
            return loadFeed();
          })
          .catch(function (error) {
            window.alert("Bild konnte nicht gelöscht werden: " + (error.message || error));
          });
        return;
      }

      if (target.closest("[data-viewer-prev='1']")) {
        event.preventDefault();
        moveActive(-1);
        return;
      }

      if (target.closest("[data-viewer-next='1']")) {
        event.preventDefault();
        moveActive(1);
        return;
      }

      if (target.closest("[data-viewer-compare='1']")) {
        event.preventDefault();
        if (state.viewerIndices.length > 1) exitSplit();
        else splitFromSingle();
        return;
      }

      var pane = target.closest(".physique-viewer-pane");
      if (!pane) return;
      event.preventDefault();
      var paneIndex = Number(pane.getAttribute("data-pane-index") || 0);
      state.activePane = paneIndex === 1 ? 1 : 0;
      renderViewer();
    });

    var zoomGestures = [null, null];
    var lastPaneTap = [0, 0];
    function touchDistance(a, b) {
      var dx = a.clientX - b.clientX;
      var dy = a.clientY - b.clientY;
      return Math.sqrt(dx * dx + dy * dy);
    }
    paneEls.forEach(function (pane, paneIndex) {
      pane.addEventListener("touchstart", function (event) {
        if (!state.viewerOpen) return;
        state.activePane = paneIndex;
        if (event.touches.length === 2) {
          zoomGestures[paneIndex] = {
            mode: "pinch",
            distance: touchDistance(event.touches[0], event.touches[1]),
            scale: state.zoom[paneIndex].scale,
          };
          return;
        }
        if (event.touches.length === 1 && state.zoom[paneIndex].scale > 1.01) {
          zoomGestures[paneIndex] = {
            mode: "pan",
            x: event.touches[0].clientX,
            y: event.touches[0].clientY,
            originX: state.zoom[paneIndex].x,
            originY: state.zoom[paneIndex].y,
          };
        }
      }, { passive: true });

      pane.addEventListener("touchmove", function (event) {
        var gesture = zoomGestures[paneIndex];
        if (!gesture) return;
        event.preventDefault();
        event.stopPropagation();
        if (gesture.mode === "pinch" && event.touches.length === 2) {
          var ratio = touchDistance(event.touches[0], event.touches[1]) / Math.max(1, gesture.distance);
          state.zoom[paneIndex].scale = Math.max(1, Math.min(5, gesture.scale * ratio));
          if (state.zoom[paneIndex].scale <= 1.01) resetZoom(paneIndex);
          applyZoom(paneIndex);
          return;
        }
        if (gesture.mode === "pan" && event.touches.length === 1) {
          state.zoom[paneIndex].x = gesture.originX + event.touches[0].clientX - gesture.x;
          state.zoom[paneIndex].y = gesture.originY + event.touches[0].clientY - gesture.y;
          applyZoom(paneIndex);
        }
      }, { passive: false });

      pane.addEventListener("touchend", function (event) {
        var gesture = zoomGestures[paneIndex];
        if (gesture) {
          event.stopPropagation();
          if (!event.touches.length) zoomGestures[paneIndex] = null;
          return;
        }
        if (event.changedTouches.length !== 1) return;
        var now = Date.now();
        if (now - lastPaneTap[paneIndex] < 300) {
          event.preventDefault();
          event.stopPropagation();
          if (state.zoom[paneIndex].scale > 1.01) resetZoom(paneIndex);
          else state.zoom[paneIndex] = { scale: 2.5, x: 0, y: 0 };
          applyZoom(paneIndex);
          lastPaneTap[paneIndex] = 0;
          return;
        }
        lastPaneTap[paneIndex] = now;
      }, { passive: false });
    });

    var touchStart = null;
    var viewerGrid = viewer.querySelector(".physique-viewer-grid");
    if (viewerGrid) {
      viewerGrid.addEventListener("touchstart", function (event) {
        if (!state.viewerOpen || !event.touches || event.touches.length !== 1) return;
        var touch = event.touches[0];
        var pane = event.target instanceof Element ? event.target.closest(".physique-viewer-pane") : null;
        touchStart = {
          x: touch.clientX,
          y: touch.clientY,
          at: Date.now(),
          pane: pane ? Number(pane.getAttribute("data-pane-index") || 0) : state.activePane,
        };
      }, { passive: true });

      viewerGrid.addEventListener("touchend", function (event) {
        if (!touchStart || !event.changedTouches || event.changedTouches.length !== 1) {
          touchStart = null;
          return;
        }
        var touch = event.changedTouches[0];
        var dx = touch.clientX - touchStart.x;
        var dy = touch.clientY - touchStart.y;
        var elapsed = Date.now() - touchStart.at;
        var startPane = touchStart.pane;
        touchStart = null;
        if (state.zoom[startPane] && state.zoom[startPane].scale > 1.01) return;
        if (elapsed > 900 || Math.max(Math.abs(dx), Math.abs(dy)) < 48) return;
        event.preventDefault();
        if (Math.abs(dx) > Math.abs(dy)) {
          if (state.viewerIndices.length > 1) state.activePane = startPane === 1 ? 1 : 0;
          moveActive(dx < 0 ? 1 : -1);
          return;
        }
        if (dy < 0 && state.viewerIndices.length === 1) {
          splitFromSingle();
          return;
        }
        if (dy > 0 && state.viewerIndices.length > 1) exitSplit();
      }, { passive: false });
    }

    document.addEventListener("keydown", function (event) {
      if (!state.viewerOpen) return;
      var t = event.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;

      if (event.key === "Escape") {
        event.preventDefault();
        closeViewer();
        return;
      }
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        moveActive(-1);
        return;
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        moveActive(1);
        return;
      }
      if ((event.key === "ArrowUp" || event.key === "ArrowDown") && state.viewerIndices.length > 1) {
        event.preventDefault();
        state.activePane = state.activePane === 0 ? 1 : 0;
        renderViewer();
        return;
      }
      if (event.key === "c" || event.key === "C" || event.key === "Enter") {
        event.preventDefault();
        if (state.viewerIndices.length > 1) exitSplit();
        else splitFromSingle();
      }
    });

    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (statusEl) statusEl.textContent = "Upload läuft ...";

        var body = new FormData(form);
        body.append("csrf_token", csrfToken);

        fetch("/api/physique/updates", {
          method: "POST",
          body: body,
          credentials: "same-origin",
          headers: {
            "X-CSRF-Token": csrfToken,
            "X-Requested-With": "XMLHttpRequest",
          },
        }).then(function (res) {
          return res.json().then(function (data) {
            if (!res.ok || !data.ok) throw new Error(data.error || "upload_failed");
            form.reset();
            if (dropTitle) dropTitle.textContent = "Bilder wählen";
            if (statusEl) statusEl.textContent = "Gespeichert.";
            return loadFeed();
          });
        }).catch(function (error) {
          if (statusEl) statusEl.textContent = "Fehler: " + (error.message || error);
        });
      });
    }

    loadFeed().catch(function (error) {
      countLabel.textContent = "Fehler";
      feed.innerHTML = '<div class="physique-empty">Galerie konnte nicht geladen werden: ' + esc(error.message || error) + '</div>';
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
