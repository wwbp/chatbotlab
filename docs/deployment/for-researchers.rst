Deplopyment Guide for Researchers
=================================

This guide walks you through deploying ChatbotLab to AWS so your participants can access the chatbot from any browser. No programming experience is required. The entire process takes about 30 minutes of your time (plus ~20 minutes of automated setup), and you only have to do it once.

**What you get:** a private, fully functional chatbot system at a URL you can share with study participants, with an admin panel where you configure the chatbot's personality, system prompt, and behavior.

**What it costs:** roughly $30-80/month depending on traffic, billed directly to your AWS account. A typical single-study deployment stays toward the low end of that range.


Before you begin
----------------

You will need to collect or create five things. Each section below tells you exactly where to get them.

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - What you need
     - Where it comes from
     - Becomes this secret
   * - AWS access key ID
     - AWS IAM console
     - ``AWS_ACCESS_KEY_ID``
   * - AWS secret access key
     - AWS IAM console
     - ``AWS_SECRET_ACCESS_KEY``
   * - OpenAI API key
     - platform.openai.com
     - ``OPENAI_API_KEY``
   * - Anthropic API key *(optional)*
     - console.anthropic.com
     - ``ANTHROPIC_API_KEY``
   * - Database password *(you choose)*
     - Make one up
     - ``DB_PASSWORD``
   * - Admin panel password *(you choose)*
     - Make one up
     - ``ADMIN_PANEL_PASSWORD``
   * - GitHub personal access token
     - GitHub settings
     - ``GH_PAT``
   * - Custom domain *(optional)*
     - Your registrar
     - ``DOMAIN_NAME``


Step 1: Fork the repository
---------------------------

Go to the ChatbotLab repository on GitHub and click **Fork** (top right). Accept the defaults and click **Create fork**. All subsequent steps happen inside your fork.

Step 2: Create an AWS account
-----------------------------

If you already have an AWS account, skip to Step 3.

1. Go to `aws.amazon.com <https://aws.amazon.com>`_ and click **Create an AWS Account**
2. Enter your email, choose an account name, and follow the prompts
3. You will need a credit card. AWS charges only for what you use
4. Complete phone verification and choose the **Basic support plan** (free)

.. note::

   **University researchers:** Many institutions have AWS credits or a shared research computing account. Check with your IT department before creating a new account.


Step 3: Create AWS access keys
-------------------------------

This gives the deployment permission to create infrastructure on your behalf. You only need these keys during the initial setup. You can delete them from AWS afterward.

1. Log into the `AWS Console <https://console.aws.amazon.com>`_
2. Search for **IAM** in the top search bar and click it
3. In the left sidebar click **Users**, then **Create user**
4. Enter any username (e.g. ``chatbot-deployer``), click **Next**
5. Select **Attach policies directly**, search for **AdministratorAccess**, check the box, click **Next**, then **Create user**
6. Click on the user you just created, go to the **Security credentials** tab
7. Scroll to **Access keys** → **Create access key**
8. Select **Other**, click **Next**, then **Create access key**
9. Copy both values now. **You will not be able to see the secret again:**

   - **Access Key ID** → ``AWS_ACCESS_KEY_ID``
   - **Secret Access Key** → ``AWS_SECRET_ACCESS_KEY``

.. note::

   **Security note:** These keys have full admin access to your AWS account. Once the deployment finishes successfully, you can delete the IAM user entirely. The running system uses a different, more limited credential, created automatically during setup.

Step 4: Get an AI provider API key
------------------------------------

You need at least one of the following.

**OpenAI (GPT models):**

1. Go to `platform.openai.com <https://platform.openai.com>`_ and sign in or create an account
2. Click your profile icon → **API keys** → **Create new secret key**
3. Give it a name, click **Create secret key**
4. Copy the key → ``OPENAI_API_KEY``

**Anthropic (Claude models), optional:**

1. Go to `console.anthropic.com <https://console.anthropic.com>`_ and sign in or create an account
2. Click **API Keys** in the left sidebar → **Create Key**
3. Copy the key → ``ANTHROPIC_API_KEY``

You only need one. If you set both, the chatbot can use either model.

Step 5: Choose two passwords
-----------------------------

These do not need to be retrieved from anywhere. You create them yourself.

**Database password (``DB_PASSWORD``):**
A password for the internal database. You will rarely need to type this. Rules: letters and numbers only (no special characters), at least 8 characters.
Example: ``Research2024db``

**Admin panel password (``ADMIN_PANEL_PASSWORD``):**
The password you will use to log into the chatbot configuration panel. Choose something you will remember.
Example: ``MyStudyAdmin99``

Step 6: Create a GitHub Personal Access Token
----------------------------------------------

This lets the deployment automatically configure your repository after the infrastructure is created. You never have to copy and paste infrastructure details manually.

1. On GitHub, click your profile picture (top right) → **Settings**
2. Scroll to the bottom of the left sidebar → **Developer settings**
3. Click **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
4. Give it a name (e.g. ``chatbot-deploy``)
5. Set **Expiration** to 90 days or longer
6. Under **Repository access**, select **Only select repositories** and choose your fork
7. Under **Permissions → Repository permissions**:

   - Find **Secrets** and set it to **Read and write**
   - (Actions and Metadata are already Read by default. Leave them.)

8. Click **Generate token**
9. Copy the token → ``GH_PAT``

.. note::

   **Alternative:** If you prefer a simpler setup, generate a **Classic token** instead (Personal access tokens → Tokens (classic)) and check just the ``repo`` scope. One checkbox covers everything.

.. important::

   The token expires. If you need to re-run the deployment workflow after the token expires, you will need to generate a new one and update the ``GH_PAT`` secret.

Step 7: Custom Domain (Optional)
----------------------------------

If you want the chatbot served at your own URL (e.g. ``chatbot.mylab.org``) instead of the auto-assigned ``https://xxxx.cloudfront.net``, you need a domain name. You can purchase one from any registrar (Namecheap, Google Domains, GoDaddy, etc.) for roughly $10-15/year.

If you are at a university, your IT department may be able to provide a subdomain under your institution's domain.

If you skip this, the chatbot is fully functional. It just has a less memorable URL. You can always add a custom domain later by setting the ``DOMAIN_NAME`` secret and re-running the workflow.

Step 8: Add secrets to your GitHub repository
----------------------------------------------

Go to your fork on GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add each of the following secrets one at a time:

.. list-table::
   :header-rows: 1
   :widths: 35 40 25

   * - Secret name
     - Value
     - Required?
   * - ``AWS_ACCESS_KEY_ID``
     - From Step 3
     - Yes
   * - ``AWS_SECRET_ACCESS_KEY``
     - From Step 3
     - Yes
   * - ``OPENAI_API_KEY``
     - From Step 4
     - At least one AI key
   * - ``ANTHROPIC_API_KEY``
     - From Step 4
     - At least one AI key
   * - ``DB_PASSWORD``
     - Password you chose in Step 5
     - Yes
   * - ``ADMIN_PANEL_PASSWORD``
     - Password you chose in Step 5
     - Yes
   * - ``GH_PAT``
     - From Step 6
     - Yes
   * - ``TF_PROJECT_PREFIX``
     - A short name for your deployment (e.g. ``mylab-chatbot``)
     - Yes
   * - ``DOMAIN_NAME``
     - Your domain (e.g. ``chatbot.mylab.org``)
     - No

.. important::

   ``TF_PROJECT_PREFIX`` is prepended to the name of every AWS resource the
   deployment creates, so pick something short, lowercase, and recognisable as
   yours — ``mylab-chatbot`` is a good shape. Use letters, digits and hyphens
   only.

   **Set it once and never change it.** Changing it later makes the next run
   look for a differently-named database, cache and storage bucket, find
   nothing, and build a second complete set alongside the first — which you
   then pay for twice. There is deliberately no default: a wrong value here
   would point the deployment at the wrong infrastructure, so the workflow
   stops rather than guess.

Step 9: Run the deployment
---------------------------

1. Go to your fork → **Actions** tab
2. Click **Deploy Infrastructure** in the left sidebar
3. Click **Run workflow** (top right of the workflow list)
4. A small form appears. Leave all fields at their defaults unless you have a reason to change them:

   - **App server instance type:** ``t3.small``. Handles ~50 simultaneous conversations. Upgrade to ``t3.medium`` for larger studies.
   - **Maximum number of app server instances:** ``1``. Increase for studies with hundreds of simultaneous participants.
   - **Database instance class:** ``db.t3.micro``. Sufficient for most studies.

5. Click the green **Run workflow** button
6. Click into the running workflow to watch progress

The workflow takes approximately **20-25 minutes**. The database and cache take the longest to provision. Once infrastructure is ready, the application deploys automatically. You do not need to click anything else.

**If you set a custom domain:** partway through, the workflow Summary will show a DNS record to add at your registrar. Add it while the workflow is still running. The workflow will wait up to 30 minutes for DNS to propagate.

Step 10: After deployment
--------------------------

When the workflow finishes, click the **Summary** tab inside the completed run. You will see:

.. code-block:: text

   Chatbot:      https://xxxx.cloudfront.net
   Admin panel:  https://xxxx.cloudfront.net/api/admin/

1. Open the **Admin panel** URL
2. Log in with username ``admin`` and your ``ADMIN_PANEL_PASSWORD``
3. Configure your chatbot: system prompt, persona, typing delays, message chunking
4. Share the **Chatbot** URL with your study participants, or embed it in your Qualtrics/Prolific survey


Ongoing use
-----------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Task
     - How
   * - Change the system prompt or persona
     - Log into the admin panel. No redeployment needed.
   * - Deploy updated app code
     - Push to the ``main`` branch. Deploys automatically.
   * - Scale up for a large study
     - Re-run **Deploy Infrastructure** with a larger instance type or more max instances
   * - Rotate an API key
     - Update the secret in GitHub Settings, then re-run **Deploy Infrastructure**
   * - Add a custom domain after initial deploy
     - Set ``DOMAIN_NAME`` secret, re-run **Deploy Infrastructure**
   * - Shut down and stop all charges
     - See "Shutting down" below


Shutting down
-------------

To tear down all AWS infrastructure and stop charges:

.. warning::

   **Data warning:** Destroying the infrastructure deletes the database and all conversation data permanently. Export any data you need from the admin panel first.

Contact your lab's technical contact to run ``terraform destroy`` from the ``infra/terraform/`` directory. This removes all AWS resources created by the deployment.


Troubleshooting
---------------

**The workflow failed partway through.**
The workflow automatically rolls back and destroys any partially-created resources. Fix the issue (usually a missing or incorrect secret) and re-run the workflow from scratch.

**The admin panel will not log in.**
Make sure you are going to ``/api/admin/`` (not just ``/admin/``). The username is ``admin`` and the password is the value you set as ``ADMIN_PANEL_PASSWORD``.

**The chatbot URL shows an error after deployment.**
Application startup takes 5-10 minutes after infrastructure provisioning. Wait a few minutes and refresh. If the error persists, check the **Deploy Humanlike-Bot Client and Server to Production** workflow in the Actions tab.

**The GH_PAT token expired and the workflow failed.**
Generate a new fine-grained token (Step 6), update the ``GH_PAT`` secret in GitHub Settings, and re-run the workflow.
