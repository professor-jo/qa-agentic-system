### React
- Render with @testing-library/react (render, screen, fireEvent/userEvent); jest-dom matchers are loaded.
- Wrap components in the providers they need (context providers, routers such as MemoryRouter).
- Mock API modules with vi.mock("../../../src/api.js", () => ({ ... })) instead of real HTTP.
- Query by role/text (getByRole("button", { name: /save/i })), not by class names.
