(function () {
  var dataEl = document.getElementById("partnerships-data");
  var partnershipContentTypes = dataEl ? JSON.parse(dataEl.textContent) : {};

  function updateContentTypes(partnershipInput) {
    var row = partnershipInput.closest(".queue-row");
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
