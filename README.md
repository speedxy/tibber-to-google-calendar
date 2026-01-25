# **Tibber to Google Calendar**

This script retrieves current electricity prices from [Tibber](https://tibber.com) and synchronizes relevant price periods (Cheap / Normal / Expensive) as events into a Google Calendar. This allows for easy visualization of electricity costs directly within your calendar app, facilitating the scheduling of high-consumption appliances.

## **Prerequisites**

Before setting up the script, ensure you have the following:

* Python 3.9 or higher installed.  
* A Tibber account with access to the Developer API.  
* A Google Cloud project with the Calendar API enabled.  
* Access to a terminal (Linux, macOS, or WSL on Windows).

## **Installation and Setup**

### **1. Clone Repository and Prepare Environment**

Perform these steps on your computer or server:

```bash
git clone https://github.com/speedxy/tibber-to-google-calendar.git
cd tibber-to-google-calendar

# Create and activate virtual environment  
python3 -m venv .venv  
source .venv/bin/activate

# Install dependencies  
pip install -r requirements.txt
```

### **2. Configuration (config.json)**

Copy the example configuration and insert your credentials:

```bash
cp config.example.json config.json
```

**Contents of `config.json`:**

* `TIBBER_API_KEY`: Your personal token from [developer.tibber.com](https://developer.tibber.com/).  
* `GOOGLE_CALENDAR_ID`: The ID of your target calendar (found in calendar settings under "Integration").

### **3. Google Service Account Setup**

A Service Account is required for automated access without the need for regular manual browser logins.

1. **Project Creation**: Go to the [Google Cloud Console](https://console.cloud.google.com/).  
2. **Enable API**: Search for "Google Calendar API" and enable it for your project.  
3. **Create Service Account**:  
   * Navigate to **IAM & Admin > Service Accounts**.  
   * Click **+ Create Service Account** and provide a name (e.g., `tibber-calendar-bot`).  
   * Complete the process by clicking **Done**.  
4. **Create JSON Key**:  
   * Click on the created Service Account in the list.  
   * Navigate to the **Keys** tab > **Add Key > Create new key**.  
   * Select **JSON** format. The file will be downloaded to your machine.  
5. **Integration**:  
   * Rename the downloaded file to `service_account.json`.  
   * Move it to the root directory of this project.  
   * **Note**: This file is included in `.gitignore` and must never be uploaded to public repositories.

### **4. Calendar Sharing**

The Service Account must be explicitly authorized to access your target calendar:

1. Open `service_account.json` and copy the email address found under `"client_email"`.  
2. Open Google Calendar in your browser.  
3. Go to the settings of the target calendar under **"Settings and sharing"**.  
4. Under **"Share with specific people or groups"**, add the Service Account email address.  
5. Set permissions to **"Make changes to events"**.

## **Usage**

Start the script manually to verify functionality:

```bash
source .venv/bin/activate  
python tibber-to-google-calendar.py
```

The script will delete existing entries in the current timeframe created by the script and insert new price data.

## **Automation (Cronjob)**

To run the script automatically every day (e.g., at 18:00), you can create a cronjob:

```bash
crontab -e
```

**Example entry** (ensure absolute paths are used):

```bash
0 18 * * * /path/to/project/.venv/bin/python /path/to/project/tibber-to-google-calendar.py >> /path/to/project/cron.log 2>&1
```

## **Development**

* **Python Version**: Tested with Python 3.9, 3.10, and 3.11.  
* **Secrets**: Files like `service_account.json` and `config.json` are excluded from tracking via `.gitignore`.  
* **Dependencies**: If you change the utilized libraries, update the requirements file using: `pip freeze > requirements.txt`.
* **Note**: Internal logging and terminal output are currently in German.