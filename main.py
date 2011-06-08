import tornado.ioloop
import tornado.web
from tornado import httpclient 

import urllib
import re
import json
import os

import pymongo
import bson

import config
import items

CALLBACK_URL = urllib.quote(config.REDIRECT_URL + "/callback")
LOGIN_URL = "https://www.facebook.com/dialog/oauth?client_id=%s&redirect_uri=%s&scope=email,user_checkins,publish_checkins,manage_friendlists" % (config.FACEBOOK_APPLICATION_ID, CALLBACK_URL)
ACCESS_TOKEN_URL_TPL = "https://graph.facebook.com/oauth/access_token?client_id=" + config.FACEBOOK_APPLICATION_ID \
  + "&redirect_uri=" + CALLBACK_URL \
  + "&client_secret=" + config.FACEBOOK_APPLICATION_SECRET \
  + "&code="
  
API = {
    "base"      : "https://graph.facebook.com/%s",
    "profile"   : "https://graph.facebook.com/me?access_token=%s",
    "places"    : "https://graph.facebook.com/search?type=place&center=%(lat)s,%(lon)s&distance=%(distance)d&access_token=%(access_token)s"
}

connection = pymongo.Connection()
db = connection.mobsq_db

def get_user(user_id):
    """ Helper method for fetching the user from a user_id """
    return db.profiles.find_one({"_id" : bson.objectid.ObjectId(user_id)})
    
def require_facebook_login(f):
    """ 
        This decorator forces a handler to require a Facebook login by checking 
        for a user_id and passing it as a keyword argument.
    """
    def new_f(self, *args):
        user_id = self.get_secure_cookie("user_id")
        if not user_id:
            # Probably want to do something here
            self.redirect("/login")
            self.finish()
        else:
            f(self, *args)
    return new_f
        
    

class MainHandler(tornado.web.RequestHandler):
    """ Renders the main page """
    @require_facebook_login
    def get(self):
        self.render("templates/main.html", user_id=user_id)
        
class LoginHandler(tornado.web.RequestHandler):
    """ Redirects the user to the Facebook login URL to get authorization for our app """
    def get(self):
        self.redirect(LOGIN_URL)
        
ACCESS_TOKEN_REGEX = re.compile("access_token=(.*)&expires=(.*)")
class OnLoginHandler(tornado.web.RequestHandler):
    """
        This handler takes care of logins after the user has authorized our 
        Application on Facebook. It's a 3 step process. The callback flow
        goes something like this:
        
        1. Parse out the code from the callback. This is passed as an
           URL parameter "code"
           
        2. Make an asynchronous request to Facebook to authorize this
           code. We do this by signing passing our secret key along.
           From this, we receive an auth token for making API calls.
           
        3. Make another asynchronous request to Facebook to fetch
           profile details. Save this to MongoDB. Use the autogenerated
           BSON ObjectId as our session key.
           
        TODO: Expire old sessions. Auth tokens have an expiry, this should
        be persisted somewhere.
    """
    
    @tornado.web.asynchronous    
    def get(self):
        # Store this somewhere
        code = self.get_argument("code")
        access_token_url = ACCESS_TOKEN_URL_TPL + code
        client = httpclient.AsyncHTTPClient()                        
        client.fetch(access_token_url, self.on_fetched_token)
        
    def on_fetched_token(self, response):
        """ Callback inokved when the auth_token is fetched """
        if response.error:
            print "Error:", response.error
        else:
            body = response.body
            matches = ACCESS_TOKEN_REGEX.search(body)
            if matches:
                access_token = matches.group(1)
                print "Access token: %s" % access_token
                client = httpclient.AsyncHTTPClient()                        
                # lambda is effectively a function factory for us
                client.fetch(API["profile"] % access_token, lambda response: self.on_profile_fetch(response, access_token))      
                
    def on_profile_fetch(self, response, access_token):
        """ Callback invoked when we have fetched the user's profile """
        if response.error:        
            print "Error:", response.error
        else:
            profile = json.loads(response.body)
            profile["access_token"] = access_token
            print "Writing profile: %s" % profile
            profile_id = db.profiles.insert(profile, safe=True)
            print "Wrote profile with ID: %s" % profile_id
            self.set_secure_cookie("user_id", str(profile_id))
            self.write("Cookie set.")
            self.finish()
  
class NearbyLocationsHandler(tornado.web.RequestHandler):
    """ 
        Serves locations nearby. Returns JSON. This is typically invoked
        via an XHR get because the browser needs to use the JavaScript
        geolocation API to determine the current user's lat/long. Makes
        use of Facebook's Places API, then maps these to game metadata
        associated with each of the places returned.
    """
    
    @tornado.web.asynchronous    
    @require_facebook_login
    def get(self):
        user_id = self.get_secure_cookie("user_id")
        lat = self.get_argument("lat")
        lon = self.get_argument("lon")        
        user = get_user(user_id)
        
        url = API["places"] % { "lat" : lat, 
                                "lon" : lon,
                                "distance" : 1000,
                                "access_token" : user["access_token"] }
        
        client = httpclient.AsyncHTTPClient()                        
        client.fetch(url, self.on_fetch_places)
            
    def on_fetch_places(self, response):
        """ Callback invoked after places fetched """
        places = json.loads(response.body)
        # Add additional metadata we've stored locally
        self.write(json.dumps(places))
        self.finish()
        
class LocationHandler(tornado.web.RequestHandler):
    """ 
        Returns data about a particular location. This Handler is invoked at
        /location/LOCATION_ID, where LOCATION_ID maps to an ID of the location
        as specified in Facebook's Graph API. Merge with local data.
        
        POST handler updates this location.
        
        Renders an HTML page.
    """
    
    @tornado.web.asynchronous
    @require_facebook_login
    def get(self, location_id):
        url = API["base"] % location_id
        client = httpclient.AsyncHTTPClient()
        client.fetch(url, self.on_fetch_location)
            
    def on_fetch_location(self, response):
        """ Callback invoked when we get location data """
        location = json.loads(response.body)
        self.render("templates/location.html", location=location)        
        self.finish()
        
class StoreHandler(tornado.web.RequestHandler):
    """
        Handler for store and inventory actions.
    """
    @require_facebook_login
    def get(self):
        """ 
            Fetches the purchasable items from the datastore and renders
            them to the user via HTML.
        """
        self.render("templates/store.html", weapons=items.weapons, armor=items.armor)
        
    def post(self):
        """
            Allows a user to make purchases
        """
        pass


application = tornado.web.Application([
    (r"/", MainHandler),
    (r"/login", LoginHandler),
    (r"/callback", OnLoginHandler),
    (r"/nearby", NearbyLocationsHandler),
    (r"/location/([0-9]+)", LocationHandler),
    (r"/store", StoreHandler)
], cookie_secret=config.COOKIE_SECRET,
   static_path=os.path.join(os.path.dirname(__file__), "static"),
   debug=True)

if __name__ == "__main__":
    application.listen(8888)
    tornado.ioloop.IOLoop.instance().start()