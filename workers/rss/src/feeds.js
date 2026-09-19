// Where the feed list comes from at poll time: the D1 `sources` table, which
// `/settings` and `cyris sources push` write, so adding a feed is a write rather
// than a redeploy. There is no bundled fallback — a fork would poll feeds nobody
// chose — so an empty table polls nothing and says so in the Worker's log.
export async function loadFeeds(env) {
  try {
    const { results } = await env.DB.prepare(
      `SELECT name, url FROM sources WHERE type = 'rss' AND url IS NOT NULL ORDER BY name`
    ).all();
    if (!results?.length) {
      console.error(
        "sources table is empty; polling nothing. Add a source on /settings or run `cyris sources push`."
      );
    }
    return results ?? [];
  } catch (error) {
    console.error(`could not read sources from D1 (${error}); polling nothing`);
    return [];
  }
}
