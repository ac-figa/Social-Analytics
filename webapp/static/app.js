(function () {
  var dataEl = document.getElementById("partnerships-data");
  var partnershipContentTypes = dataEl ? JSON.parse(dataEl.textContent) : {};

  function updateContentTypes(partnershipInput) {
    var row = partnershipInput.closest("form");
    if (!row) return;
    var contentTypeInput = row.querySelector(".content-type-input");
    if (!contentTypeInput) return;
    var datalist = document.getElementById(contentTypeInput.getAttribute("list"));
    if (!datalist) return;

    var types = partnershipContentTypes[partnershipInput.value] || [];
    datalist.innerHTML = "";
    types.forEach(function (t) {
      var opt = document.createElement("option");
      opt.value = t;
      datalist.appendChild(opt);
    });
  }

  document.querySelectorAll(".partnership-input").forEach(function (input) {
    updateContentTypes(input);
    input.addEventListener("input", function () {
      updateContentTypes(input);
    });
  });
})();

// "Apply All" -- gathers every filled-in row on the classify queue and
// posts them all in one request, instead of one page reload per row.
(function () {
  var btn = document.getElementById("apply-all-btn");
  if (!btn) return;
  var status = document.getElementById("apply-all-status");

  btn.addEventListener("click", function () {
    var rows = [];
    document.querySelectorAll(".queue-row").forEach(function (row) {
      var partnership = row.querySelector(".partnership-input");
      var contentType = row.querySelector(".content-type-input");
      if (!partnership || !partnership.value.trim()) return;
      rows.push({
        group_id: row.getAttribute("data-group-id") || null,
        content_id: row.getAttribute("data-content-id") || null,
        platform: row.getAttribute("data-platform") || null,
        platform_post_id: row.getAttribute("data-platform-post-id") || null,
        partnership: partnership.value.trim(),
        content_type: contentType ? contentType.value.trim() : "",
      });
    });

    if (!rows.length) {
      if (status) status.textContent = "Nothing filled in yet.";
      return;
    }

    btn.disabled = true;
    if (status) status.textContent = "Applying " + rows.length + "...";

    fetch(btn.getAttribute("data-action") || "/classify/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows: rows }),
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error("Request failed");
        return resp.json();
      })
      .then(function () {
        window.location.reload();
      })
      .catch(function () {
        btn.disabled = false;
        if (status) status.textContent = "Something went wrong -- try again.";
      });
  });
})();

// Manual grouping -- a checkbox on any row in Browse or the Classify
// queue adds that row's group/item token to a selection kept in
// localStorage (so it survives navigating between Browse's platform tabs),
// shown as a floating bar with a "Group Selected" button that posts the
// whole selection to /group in one go.
(function () {
  var STORAGE_KEY = "socialAnalyticsGroupSelection";
  var bar = document.getElementById("group-selection-bar");
  if (!bar) return;
  var countEl = document.getElementById("group-selection-count");
  var applyBtn = document.getElementById("group-selection-apply");
  var clearBtn = document.getElementById("group-selection-clear");
  var form = document.getElementById("group-selection-form");

  function load() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    } catch (e) {
      return [];
    }
  }

  function save(selection) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(selection));
    } catch (e) {
      /* ignore -- private browsing / storage disabled */
    }
  }

  function render() {
    var selection = load();
    if (selection.length === 0) {
      bar.classList.add("hidden");
      return;
    }
    bar.classList.remove("hidden");
    countEl.textContent = selection.length + " selected for grouping";

    document.querySelectorAll(".group-checkbox").forEach(function (cb) {
      var token = cb.getAttribute("data-token");
      cb.checked = selection.some(function (s) {
        return s.token === token;
      });
    });
  }

  document.addEventListener("change", function (e) {
    if (!e.target.classList || !e.target.classList.contains("group-checkbox")) return;
    var token = e.target.getAttribute("data-token");
    var label = e.target.getAttribute("data-label") || token;
    var selection = load();
    if (e.target.checked) {
      if (!selection.some(function (s) { return s.token === token; })) {
        selection.push({ token: token, label: label });
      }
    } else {
      selection = selection.filter(function (s) { return s.token !== token; });
    }
    save(selection);
    render();
  });

  clearBtn.addEventListener("click", function () {
    save([]);
    render();
  });

  applyBtn.addEventListener("click", function () {
    var selection = load();
    if (selection.length < 2) {
      alert("Select at least 2 items (from different platforms) to group.");
      return;
    }
    form.innerHTML = "";
    selection.forEach(function (s) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "selections";
      input.value = s.token;
      form.appendChild(input);
    });
    save([]);
    form.submit();
  });

  render();
})();
