(function () {
    const STORAGE_KEY = "policedesk-theme";
    const root = document.documentElement;

    const toggleButton = document.querySelector("[data-theme-toggle]");
    const themeIcon = document.querySelector("[data-theme-icon]");
    const themeLabel = document.querySelector("[data-theme-label]");

    function getPreferredTheme() {
        const savedTheme = localStorage.getItem(STORAGE_KEY);

        if (savedTheme === "light" || savedTheme === "dark") {
            return savedTheme;
        }

        const prefersDark = window.matchMedia &&
            window.matchMedia("(prefers-color-scheme: dark)").matches;

        return prefersDark ? "dark" : "light";
    }

    function applyTheme(theme) {
        root.setAttribute("data-theme", theme);
        localStorage.setItem(STORAGE_KEY, theme);

        if (themeIcon) {
            themeIcon.textContent = theme === "dark" ? "🌙" : "☀️";
        }

        if (themeLabel) {
            themeLabel.textContent = theme === "dark" ? "Dark" : "Light";
        }

        if (toggleButton) {
            toggleButton.setAttribute(
                "aria-label",
                theme === "dark" ? "Switch to light mode" : "Switch to dark mode"
            );
        }
    }

    function toggleTheme() {
        const currentTheme = root.getAttribute("data-theme") || "light";
        const nextTheme = currentTheme === "dark" ? "light" : "dark";

        applyTheme(nextTheme);
    }

    const initialTheme = getPreferredTheme();
    applyTheme(initialTheme);

    if (toggleButton) {
        toggleButton.addEventListener("click", toggleTheme);
    }
})();