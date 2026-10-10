// Where the web harness finds the brain, and how people sign in.
//
// Local development: run `python -m brain.local` and open this folder with any static server.
// Deployed: set brainUrl to the Cloud Run address of the brain, and paste the Firebase web
// config (Project settings > Your apps) into `firebase` so visitors are signed in anonymously.
window.SHADOW_CONFIG = {
  brainUrl: "http://127.0.0.1:8090",
  firebase: null,
  // firebase: { apiKey: "...", authDomain: "PROJECT.firebaseapp.com", projectId: "PROJECT", appId: "..." },
};
