// Deliberately empty.
//
// PostCSS searches parent directories for a configuration, and this machine
// has a stray tailwind setup in the user's home folder. Without this file,
// that unrelated config is applied to this project and emits warnings about
// content sources it has no business scanning. Declaring no plugins here
// stops the search at the project root.
export default { plugins: {} };
