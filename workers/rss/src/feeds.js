// Where the feed list comes from at poll time.
//
// The `sources` table is written by `cyris sources push`, so adding a feed is a
// write rather than a redeploy. The bundled feeds.json stays as the fallback: a
// Worker deployed before the first push, or one whose D1 read fails, has to keep
// polling — polling nothing is a silent outage that just looks like a quiet day.
export async function loadFeeds(env, bundled) {
  try {
    const { results } = await env.DB.prepare(
      `SELECT name, url FROM sources WHERE type = 'rss' AND url IS NOT NULL ORDER BY name`
    ).all();
    if (results?.length) return results;
    console.log("sources table is empty; using the bundled feeds.json");
  } catch (error) {
    console.warn(`could not read sources from D1 (${error}); using the bundled feeds.json`);
  }
  return bundled;
}
