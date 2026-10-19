### Astro
- Render components and pages with Astro's container:
    import { experimental_AstroContainer as AstroContainer } from "astro/container";
    const c = await AstroContainer.create({ astroConfig: { site: "https://example.com" } });
    const html = await c.renderToString(Page, { props: {...}, slots: { default: "..." },
                                                request: new Request("https://example.com/about/") });
- Astro adds attributes like data-astro-cid-* to tags: assert on text (toContain("Services"))
  or attribute-tolerant regexes (/<li[^>]*>Item<\/li>/), never on exact tags such as "<li>".
- Plain .ts/.js modules (src/data/*, utils) are imported directly; test their logic first.
- Components using astro:assets images need real image imports; if that's awkward, test the page around them instead.
