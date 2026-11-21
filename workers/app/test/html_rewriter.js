// What workerd's HTMLRewriter does to a page, which Node lacks: whatever a handler appends to
// <head> goes last in it. Tests that serve a tenant's page through hosted.js stub the global with
// this, as the page now carries a vote token for a signed-in reader.
export class FakeHTMLRewriter {
  on(_selector, handlers) {
    this.handlers = handlers;
    return this;
  }

  transform(response) {
    const appended = [];
    this.handlers.element({ append: (html) => appended.push(html) });
    const body =
      response.body &&
      new ReadableStream({
        async start(controller) {
          const text = await response.text();
          controller.enqueue(new TextEncoder().encode(text.replace("</head>", `${appended.join("")}</head>`)));
          controller.close();
        },
      });
    return new Response(body, response);
  }
}
