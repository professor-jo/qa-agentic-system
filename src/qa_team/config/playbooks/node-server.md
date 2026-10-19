### Node servers (Express / Fastify)
- Import the app object without calling listen(); test routes in-process (e.g. app.inject for Fastify,
  or call handlers with mock req/res objects).
- Never hit real databases or external APIs: mock the data layer with vi.mock.
