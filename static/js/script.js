
const menuToggle = document.getElementById("menuToggle");
const navLinks = document.getElementById("navLinks");

if (menuToggle && navLinks) {
    menuToggle.addEventListener("click", () => {
        const isOpen = navLinks.classList.toggle("active");

        menuToggle.textContent = isOpen ? "✕" : "☰";
        menuToggle.setAttribute("aria-expanded", isOpen);
    });

    navLinks.querySelectorAll("a").forEach(link => {
        link.addEventListener("click", () => {
            navLinks.classList.remove("active");
            menuToggle.textContent = "☰";
            menuToggle.setAttribute("aria-expanded", "false");
        });
    });
}

const yearElement = document.getElementById("currentYear");

if (yearElement) {
    yearElement.textContent = new Date().getFullYear();
}

const accountRole = document.getElementById("accountRole");
const organizationType = document.getElementById("organizationType");
const peopleServedField = document.getElementById("peopleServedField");

function updateRegistrationFields() {
    if (!accountRole || !organizationType) {
        return;
    }

    const role = accountRole.value;
    const selectedOption = organizationType.selectedOptions[0];
    if (selectedOption && selectedOption.dataset.audience !== "both"
        && selectedOption.dataset.audience !== role) {
        organizationType.value = "";
    }

    Array.from(organizationType.options).forEach(option => {
        option.hidden = option.value !== ""
            && option.dataset.audience !== "both"
            && option.dataset.audience !== role;
    });

    if (peopleServedField) {
        peopleServedField.hidden = role !== "recipient";
        const peopleInput = peopleServedField.querySelector("input");
        peopleInput.required = role === "recipient";
    }
}

if (accountRole) {
    accountRole.addEventListener("change", updateRegistrationFields);
    updateRegistrationFields();
}
