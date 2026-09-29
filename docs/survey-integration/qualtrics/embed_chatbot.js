// Qualtrics — Edit Question JavaScript for the Text/Graphic question that
// hosts the chatbot.
//
// Where this goes: add a Text / Graphic question, open the JavaScript editor
// (Edit Question -> Question Behavior -> JavaScript), and paste this in.
//
// Replace every <PLACEHOLDER> below. Values that live in Embedded Data can be
// pulled in with Qualtrics piping, e.g. "${e://Field/pid}".
//
// NOTE: this uses addOnReady, not addOnload. addOnload runs before the
// question is fully displayed, and an element appended there is lost when
// Qualtrics renders the question — the iframe simply never appears, with no
// error. addOnReady runs once the page is displayed, which is where DOM work
// belongs.

Qualtrics.SurveyEngine.addOnReady(function () {
  var studyName = "<STUDY-NAME>"; // your label for this study
  var botName = "<BOT-NAME>"; // must match a Bot name in the admin panel
  var surveyID = "<SURVEY-ID>"; // your Qualtrics survey identifier
  var participantID = "${e://Field/ResponseID}"; // or your own embedded data field
  var userGroup = "<USER-GROUP>"; // condition label, e.g. "treatment"
  var conversationID = "${e://Field/ResponseID}"; // Qualtrics response ID

  // Construct chatbot URL with encoded parameters.
  // IMPORTANT: surveyID and participantID must match the values sent to
  // /api/survey_response/ — that pair is how the bot finds the answers.
  var botURL = "https://<YOUR-CHATBOTLAB-DOMAIN>/conversation";
  botURL += "?bot_name=" + encodeURIComponent(botName);
  botURL += "&conversation_id=" + encodeURIComponent(conversationID);
  botURL += "&participant_id=" + encodeURIComponent(participantID);
  botURL += "&study_name=" + encodeURIComponent(studyName);
  botURL += "&user_group=" + encodeURIComponent(userGroup);
  botURL += "&survey_id=" + encodeURIComponent(surveyID);

  // Check this in the browser console: an empty participant_id means the
  // piping above did not resolve, and the chat will silently never start.
  console.log("Generated botURL:", botURL);

  var container = this.getQuestionTextContainer() || this.questionContainer;
  if (!container) {
    console.error("ChatbotLab: no container found to append the iframe to.");
    return;
  }

  // Plain DOM rather than jQuery: width/height set as HTML attributes are
  // ignored when given CSS units, which renders a zero-height iframe that
  // looks identical to the script not running at all.
  var iframe = document.createElement("iframe");
  iframe.src = botURL;
  iframe.setAttribute("frameborder", "0");
  iframe.style.width = "100%";
  iframe.style.height = "700px";
  iframe.style.border = "0";
  iframe.style.display = "block";
  container.appendChild(iframe);
});
