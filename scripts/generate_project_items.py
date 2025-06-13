# scripts/generate_project_items.py
import os
import json
import sys
import requests
from github import Github, GithubException, GithubObject 
from github.Issue import Issue 
import subprocess
from datetime import datetime, timedelta

# --- 環境変数の読み込み ---
# GitHub Actionsから渡される環境変数
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_ORG_NAME = os.getenv('GITHUB_ORG_NAME')
FRONTEND_REPO_NAME = os.getenv('FRONTEND_REPO_NAME')
BACKEND_REPO_NAME = os.getenv('BACKEND_REPO_NAME')
GITHUB_PROJECT_NAME = os.getenv('GITHUB_PROJECT_NAME')

# デバッグ用: 環境変数が設定されているか確認
if not all([GEMINI_API_KEY, GITHUB_TOKEN, GITHUB_ORG_NAME, FRONTEND_REPO_NAME, BACKEND_REPO_NAME, GITHUB_PROJECT_NAME]):
    print("Error: One or more required environment variables are not set.")
    sys.exit(1)

# --- GitHub APIクライアントの初期化 ---
try:
    g = Github(GITHUB_TOKEN)
    org = g.get_organization(GITHUB_ORG_NAME)
    frontend_repo = org.get_repo(FRONTEND_REPO_NAME)
    backend_repo = org.get_repo(BACKEND_REPO_NAME)
    
    # ターゲットリポジトリのマッピング
    REPO_MAP = {
        "frontend": frontend_repo,
        "backend": backend_repo,
    }
except GithubException as e:
    print(f"Error initializing GitHub client or getting organization/repos: {e}")
    sys.exit(1)

# --- LLM API呼び出し関数 ---
def call_gemini_api(prompt_text: str) -> dict:
    """
    Gemini Pro APIを呼び出し、構造化されたJSONデータを取得する。
    """
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    payload = {
        'contents': [{'parts': [{'text': prompt_text}]}],
        'generationConfig': {
            'responseMimeType': 'application/json' # JSON形式で出力することを強制
        }
    }

    print("Calling Gemini API...")
    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=300) # タイムアウトを長めに設定
        response.raise_for_status() # HTTPエラーをチェック (4xx, 5xx)
        
        result = response.json()
        
        # LLMの出力はJSON文字列として返されるため、それをパース
        generated_json_string = result['candidates'][0]['content']['parts'][0]['text']
        print(f"Raw LLM Response JSON String: {generated_json_string}")
        return json.loads(generated_json_string) # JSON文字列をPython辞書に変換

    except requests.exceptions.RequestException as e:
        print(f"Error calling Gemini API: {e}")
        sys.exit(1)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"Error parsing LLM response or unexpected format: {e}")
        print(f"LLM raw response: {response.text if 'response' in locals() else 'No response'}")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred during LLM call: {e}")
        sys.exit(1)

# --- GitHub操作関数 ---

def get_or_create_milestone(repo, milestone_data: dict) -> int:
    """
    指定されたリポジトリにマイルストーンが存在するか確認し、なければ作成する。
    """
    milestone_name = milestone_data.get('name')
    milestone_description = milestone_data.get('description', '')
    milestone_due_on = milestone_data.get('due_on') # 例: "YYYY-MM-DD"

    if not milestone_name:
        print("Warning: Milestone name is missing. Skipping milestone creation.")
        return None

    # 既存のマイルストーンを検索
    # state='all' で開いているものと閉じているものの両方を検索
    existing_milestones = repo.get_milestones(state='all')
    for m in existing_milestones:
        if m.title == milestone_name:
            print(f"Milestone '{milestone_name}' already exists in {repo.full_name} (ID: {m.id}).")
            return m.id # 既存のマイルストーンのIDを返す

    # マイルストーンが存在しない場合、新規作成
    print(f"Creating new milestone '{milestone_name}' in {repo.full_name}...")
    
    # due_onの日付をdatetimeオブジェクトに変換
    due_on_dt = None
    if milestone_due_on:
        try:
            due_on_dt = datetime.strptime(milestone_due_on, "%Y-%m-%d")
        except ValueError:
            print(f"Warning: Invalid date format for milestone '{milestone_name}' due_on: {milestone_due_on}. Skipping due_on.")

    try:
        new_milestone = repo.create_milestone(
            title=milestone_name,
            description=milestone_description,
            due_on=due_on_dt # datetimeオブジェクトとして渡す
        )
        print(f"Successfully created milestone '{milestone_name}' in {repo.full_name} (ID: {new_milestone.id}).")
        return new_milestone.id
    except GithubException as e:
        print(f"Error creating milestone '{milestone_name}' in {repo.full_name}: {e}")
        return None

def create_github_issue(repo, issue_data: dict, milestone_id: int):
    """
    指定されたリポジトリにIssueを作成し、マイルストーンやラベルを紐付ける。
    """
    title = issue_data.get('title')
    description = issue_data.get('description', '')
    assignee_candidate = issue_data.get('assignee_candidate', 'unassigned')
    priority = issue_data.get('priority')
    task_granularity = issue_data.get('task_granularity') # タスク粒度も取得

    if not title:
        print("Warning: Issue title is missing. Skipping issue creation.")
        return

    # Issueに付与するラベルを準備
    labels_to_add = []
    if assignee_candidate != 'unassigned':
        labels_to_add.append(assignee_candidate) # ロール名をラベルとして追加
    if priority:
        labels_to_add.append(f"priority:{priority}") # 優先順位をラベルとして追加 (例: "priority:high")
    if task_granularity: # タスク粒度もラベルとして追加
        labels_to_add.append(f"granularity:{task_granularity}")


    # 既存のIssueを検索するためのマイルストーン引数を決定
    # PyGithubのget_issuesは、マイルストーンがない場合に文字列'none'を期待する
    milestone_filter_arg = None
    if milestone_id:
        try:
            milestone_filter_arg = repo.get_milestone(milestone_id)
        except GithubException as e:
            print(f"Warning: Could not retrieve milestone with ID {milestone_id} for issue '{title}' in {repo.full_name} for duplicate check: {e}. Proceeding without milestone filter for duplicate check.")
            # マイルストーンが見つからなかった場合、重複チェックではマイルストーンなしとして扱う
            milestone_filter_arg = 'none' 
    else:
        # milestone_id がNoneの場合、明示的にマイルストーンがないIssueを検索
        milestone_filter_arg = 'none'


    # 既存のIssueを検索 (簡易的な重複チェック)
    existing_issues = repo.get_issues(
        state='open',
        labels=labels_to_add, # 検索時にラベルも考慮
        milestone=milestone_filter_arg # 正しくフォーマットされたマイルストーンフィルターを渡す
    )
    for issue in existing_issues:
        if issue.title == title:
            print(f"Issue '{title}' already exists in {repo.full_name} (ID: {issue.id}). Skipping creation.")
            return

    print(f"Creating issue '{title}' in {repo.full_name}...")
    
    # Issue作成時に渡すマイルストーンオブジェクトを決定
    # ここが今回の修正の核心: milestone_id がNoneの場合、GithubObject.NotSet を渡す
    milestone_obj_for_creation = GithubObject.NotSet # デフォルト値としてNotSetを設定
    if milestone_id:
        try:
            milestone_obj_for_creation = repo.get_milestone(milestone_id)
        except GithubException as e:
            print(f"Warning: Could not retrieve milestone with ID {milestone_id} for issue creation '{title}' in {repo.full_name}: {e}. Issue will be created without milestone.")
            # 取得に失敗した場合も NotSet に戻す
            milestone_obj_for_creation = GithubObject.NotSet
        
    try:
        issue = repo.create_issue(
            title=title,
            body=description,
            labels=labels_to_add,
            milestone=milestone_obj_for_creation # Milestoneオブジェクト、またはGithubObject.NotSet を渡す
        )
        print(f"Successfully created issue '{title}' in {repo.full_name} (Issue #{issue.number}).")
        return issue
    except GithubException as e:
        print(f"Error creating issue '{title}' in {repo.full_name}: {e}")
        return None

def add_issue_to_github_project(org_name: str, project_name: str, issue_obj: Issue):
    """
    gh CLI を使用してIssueをGitHub Projectに追加する。
    """
    print(f"Adding issue #{issue_obj.number} from {issue_obj.repository.full_name} to GitHub Project '{project_name}'...")
    try:
        # Step 1: プロジェクトIDと番号を取得する
        # gh project list --owner <org_name> --format json
        list_cmd = [
            'gh', 'project', 'list',
            '--owner', org_name,
            '--format', 'json'
        ]
        
        print(f"DEBUG: Running gh project list command: {' '.join(list_cmd)}")
        list_result = subprocess.run(list_cmd, capture_output=True, text=True, check=True)
        
        print(f"DEBUG: gh project list stdout raw:\n{list_result.stdout}")
        if list_result.stderr:
            print(f"DEBUG: gh project list stderr raw:\n{list_result.stderr}")

        if not list_result.stdout.strip():
            print(f"Error: 'gh project list' returned empty stdout. No projects found or command output issue.")
            sys.exit(1)

        try:
            raw_projects_output = json.loads(list_result.stdout)
        except json.JSONDecodeError as e:
            print(f"Error: Failed to parse JSON from 'gh project list' stdout: {e}")
            print(f"  Problematic stdout content: {list_result.stdout[:500]}...")
            sys.exit(1)
        
        project_target_id = None # プロジェクトID（PVT_...）
        project_number = None    # プロジェクト番号（例: 2）

        all_projects = raw_projects_output.get('projects', []) 

        if not isinstance(all_projects, list):
            print(f"Error: Expected 'projects' key in JSON output from 'gh project list' to be a list, but got: {type(all_projects)}. Full output:\n{list_result.stdout}")
            sys.exit(1)

        for p in all_projects:
            print(f"DEBUG: Processing project object: {p}")

            if not isinstance(p, dict):
                print(f"Error: Expected project item to be a dictionary, but got {type(p)}. Content: {p}")
                sys.exit(1)

            owner_login = p.get('owner', {}).get('login')
            if owner_login == org_name and p.get('title') == project_name:
                project_target_id = p.get('id')
                project_number = p.get('number') # プロジェクト番号も取得
                break

        if not project_target_id or not project_number:
            print(f"Error: GitHub Project '{project_name}' not found for owner '{org_name}'. Please ensure the project exists and the PAT has sufficient permissions to list it.")
            print(f"Hint: You can check existing projects by running: gh project list --owner {org_name} --web")
            sys.exit(1)

        print(f"Found Project '{project_name}' with ID: {project_target_id} and Number: {project_number}")

        # Step 2: Issueをプロジェクトに追加する
        # gh project item-add <project-number> --url <issue-url>
        cmd = [
            'gh', 'project', 'item-add', str(project_number), # プロジェクト番号を文字列で渡す
            '--url', issue_obj.html_url # Issue URLを --url フラグで渡す
        ]
        
        print(f"DEBUG: Running gh project item-add command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"DEBUG: gh project item-add stdout:\n{result.stdout}")
        if result.stderr:
            print(f"DEBUG: gh project item-add stderr:\n{result.stderr}")

        print(f"Successfully added issue #{issue_obj.number} to Project '{project_name}'.")
    except subprocess.CalledProcessError as e:
        print(f"Error adding issue to GitHub Project: {e}")
        print(f"stdout: {e.stdout}")
        print(f"stderr: {e.stderr}")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred during Project linking: {e}")
        sys.exit(1)

# --- メイン処理 ---
def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_project_items.py <requirements_file_path>")
        sys.exit(1)

    requirements_file_path = sys.argv[1]

    # 1. 要件定義ファイルの読み込み
    try:
        with open(requirements_file_path, 'r', encoding='utf-8') as f:
            requirements_content = f.read()
        print(f"Successfully read requirements from: {requirements_file_path}")
    except FileNotFoundError:
        print(f"Error: requirements file not found at {requirements_file_path}")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading requirements file: {e}")
        sys.exit(1)

    # 2. LLMへのプロンプト作成
    prompt = f"""
    以下の要件定義ドキュメントから、主要なマイルストーン（目標）と、それに付随する詳細なタスク（Issue）をJSON形式で抽出してください。

    - **マイルストーン**は以下のフィールドを持つものとします。
      - `name`: マイルストーンのタイトル (文字列, 必須)
      - `description`: マイルストーンの説明 (文字列, オプション)
      - `target_repositories`: このマイルストーンが関連するリポジトリのリスト (例: `["frontend", "backend"]`)
      - `due_on`: マイルストーンの期限 (YYYY-MM-DD形式の文字列, オプション)

    - **タスク**は以下のフィールドを持つものとします。
      - `title`: Issueのタイトル (文字列, 必須)
      - `description`: Issueの説明 (文字列, オプション)
      - `target_repository`: このタスクが属するリポジトリ ('frontend' または 'backend')
      - `assignee_candidate`: 担当者候補 ('frontend' または 'backend')
      - `priority`: タスクの優先順位 ('high', 'medium', 'low' のいずれか, オプション)
      - `milestone_name`: このタスクを紐付けるマイルストーンの`name` (文字列, マイルストーンがなければ空文字列 `""`)
      - `task_granularity`: タスクの粒度 ('small' (2-4時間), 'medium' (1日-数日), 'large' (複数の詳細タスク))

    **JSONフォーマット例:**
    ```json
    {{
      "milestones": [
        {{
          "name": "GitHub OAuth 実装完了",
          "description": "ユーザーがGitHubアカウントでログインし、Supabaseと連携できる状態",
          "target_repositories": ["frontend", "backend"],
          "due_on": "{datetime.now().strftime('%Y-%m-%d')}"
        }}
      ],
      "tasks": [
        {{
          "title": "バックエンド: GitHub OAuth コールバック処理実装",
          "description": "GitHubから受け取った認証コードをSupabaseに渡し、セッションを作成。完了条件：ユーザーセッションが正常に確立されること。参考：Supabase Auth ドキュメント。",
          "target_repository": "backend",
          "assignee_candidate": "backend",
          "priority": "high",
          "milestone_name": "GitHub OAuth 実装完了",
          "task_granularity": "medium"
        }},
        {{
          "title": "フロントエンド: ログインUIと認証フロー実装",
          "description": "ログインボタンからGitHub OAuth を呼び出し、認証後のリダイレクトを処理。完了条件：ログインボタンが表示され、クリックでGitHub認証が開始されること。",
          "target_repository": "frontend",
          "assignee_candidate": "frontend",
          "priority": "high",
          "milestone_name": "GitHub OAuth 実装完了",
          "task_granularity": "medium"
        }},
        {{
          "title": "READMEを整備",
          "description": "プロジェクトの基本的な情報、目的、コンセプトを記述する",
          "target_repository": "frontend",
          "assignee_candidate": "frontend",
          "priority": "medium",
          "milestone_name": "",
          "task_granularity": "small"
        }},
        {{
          "title": "バックエンド: 草データ取得API実装",
          "description": "GitHub API を利用してユーザーのContributionデータを取得し、DBに保存するAPIを実装。影響範囲：デッキ編成画面、ユーザーデータ。",
          "target_repository": "backend",
          "assignee_candidate": "backend",
          "priority": "medium",
          "milestone_name": "",
          "task_granularity": "large"
        }}
      ]
    }}
    ```

    **要件定義ドキュメント:**
    {requirements_content}
    """

    # 3. LLM APIの呼び出し
    llm_output = call_gemini_api(prompt)

    milestones_data = llm_output.get('milestones', [])
    tasks_data = llm_output.get('tasks', [])

    # 4. マイルストーンの作成/取得 (リポジトリごとにIDを保持)
    # { "milestone_name": { "frontend": milestone_id, "backend": milestone_id } }
    created_milestone_ids = {}

    for m_data in milestones_data:
        m_name = m_data.get('name')
        if not m_name:
            print("Warning: Skipping milestone creation due to missing name in LLM output.")
            continue

        created_milestone_ids[m_name] = {}
        target_repos_for_milestone = m_data.get('target_repositories', [])
        
        for repo_key in target_repos_for_milestone:
            target_repo_obj = REPO_MAP.get(repo_key)
            if target_repo_obj:
                milestone_id = get_or_create_milestone(target_repo_obj, m_data)
                if milestone_id:
                    created_milestone_ids[m_name][repo_key] = milestone_id
                else:
                    print(f"Warning: Failed to get/create milestone '{m_name}' in {repo_key}. Associated issues might not be linked.")
            else:
                print(f"Warning: Unknown target repository '{repo_key}' for milestone '{m_name}'. Skipping.")

    # 5. タスク (Issue) の作成と紐付け
    for task_data in tasks_data:
        task_title = task_data.get('title')
        task_repo_key = task_data.get('target_repository')
        task_milestone_name = task_data.get('milestone_name', '')

        if not task_title or not task_repo_key:
            print("Warning: Skipping task creation due to missing title or target_repository.")
            continue

        target_repo_obj = REPO_MAP.get(task_repo_key)
        if not target_repo_obj:
            print(f"Warning: Unknown target repository '{task_repo_key}' for task '{task_title}'. Skipping.")
            continue
        
        # 該当するマイルストーンIDを取得
        milestone_for_issue_id = None
        if task_milestone_name and task_milestone_name in created_milestone_ids:
            milestone_for_issue_id = created_milestone_ids[task_milestone_name].get(task_repo_key)
        
        # Issueを作成
        created_issue = create_github_issue(target_repo_obj, task_data, milestone_for_issue_id)

        # Issueが正常に作成された場合、GitHub Projectに追加
        if created_issue:
            add_issue_to_github_project(
                GITHUB_ORG_NAME,
                GITHUB_PROJECT_NAME,
                created_issue # Issueオブジェクトのまま渡す
            )
        else:
            print(f"Warning: Issue '{task_title}' was not created or found. Skipping Project linking.")

    print("\nAI-powered project item generation complete!")

if __name__ == "__main__":
    main()
