// Where the feed list comes from at poll time: the D1 `sources` table, which
// `/settings` and `cyris sources push` write, so adding a feed is a write rather
// than a redeploy. There is no bundled fallback — a fork would poll feeds nobody
// chose — so an empty table polls nothing and says so in the Worker's log, and
// a table that cannot be read fails the poll rather than passing as a quiet day.
export async function loadFeeds(env) {
  let results;
  try {
    ({ results } = await env.DB.prepare(
      `SELECT name, url FROM sources WHERE type = 'rss' AND url IS NOT NULL ORDER BY name`
    ).all());
  } catch (error) {
    throw new Error(`could not read sources from D1: ${error.message ?? error}`, { cause: error });
  }
  if (!results?.length) {
    console.error(
      "sources table is empty; polling nothing. Add a source on /settings or run `cyris sources push`."
    );
  }
  return results ?? [];
}
