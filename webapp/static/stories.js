(function () {
  var tbody = document.getElementById("new-story-rows");
  var template = document.getElementById("story-row-template");
  if (!tbody || !template) return;

  var counter = 0;

  function addRow() {
    var clone = template.content.cloneNode(true);
    var datalist = clone.querySelector(".ns-content-type-datalist");
    var contentTypeInput = clone.querySelector(".content-type-input");
    if (datalist && contentTypeInput) {
      var id = "new-story-ct-" + counter++;
      datalist.id = id;
      contentTypeInput.setAttribute("list", id);
    }
    clone.querySelector(".remove-row-btn").addEventListener("click", function (e) {
      e.target.closest("tr").remove();
    });
    tbody.appendChild(clone);
  }

  document.getElementById("add-story-row-btn").addEventListener("click", addRow);

  for (var i = 0; i < 3; i++) addRow();

  document.getElementById("submit-stories-btn").addEventListener("click", function () {
    var rows = [];
    tbody.querySelectorAll(".new-story-row").forEach(function (tr) {
      var row = {};
      tr.querySelectorAll(".ns-field").forEach(function (el) {
        row[el.getAttribute("data-field")] = el.value;
      });
      rows.push(row);
    });

    var btn = document.getElementById("submit-stories-btn");
    var status = document.getElementById("submit-stories-status");
    btn.disabled = true;
    if (status) status.textContent = "Submitting...";

    fetch(btn.getAttribute("data-action"), {
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
