// Qualtrics — Edit Question JavaScript for the Text/Graphic question that
// hosts the chatbot.
//
// Where this goes: add a Text / Graphic question, open the JavaScript editor
// (Edit Question -> Question Behavior -> JavaScript), and paste this in.
//
// Replace every <PLACEHOLDER> below. Values that live in Embedded Data can be
// pulled in with Qualtrics piping, e.g. "${e://Field/pid}".

Qualtrics.SurveyEngine.addOnload(function () {
  var studyName = "<STUDY-NAME>"; // your label for this study
  var botName = "<BOT-NAME>"; // must match a Bot name in the admin panel
  var surveyID = "<SURVEY-ID>"; // your Qualtrics survey identifier
  var participantID = "<PARTICIPANT-ID>"; // e.g. "${e://Field/pid}"
  var userGroup = "<USER-GROUP>"; // condition label, e.g. "treatment"
  var conversationID = "${e://Field/ResponseID}"; // Qualtrics response ID

  window.totalTimeOnPage = 0;
  window.totalTimeAwayFromPage = 0;
  window.pageStartTime = new Date();
  window.awayStartTime = null;

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

  console.log("Generated botURL:", botURL); // Debugging

  var container = this.getQuestionTextContainer();
  if (container) {
    var iframe = jQuery("<iframe>", {
      src: botURL,
      width: "100%",
      height: "100vh",
      frameborder: "0",
    });
    jQuery(container).append(iframe);
  } else {
    alert("Error: No valid container found.");
  }

  function handleVisibilityChange() {
    var currentTime = new Date();
    if (document.hidden) {
      // User switched to a different tab
      window.awayStartTime = currentTime;
      window.totalTimeOnPage += currentTime - window.pageStartTime;
    } else {
      // User returned to the tab
      if (window.awayStartTime) {
        window.totalTimeAwayFromPage += currentTime - window.awayStartTime;
        window.awayStartTime = null;
      }
      window.pageStartTime = currentTime;
    }
  }

  handleVisibilityChange();
  document.addEventListener("visibilitychange", handleVisibilityChange, false);
});
